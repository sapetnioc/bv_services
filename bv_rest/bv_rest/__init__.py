import datetime
from collections import OrderedDict
from functools import partial, wraps
import inspect
import os.path as osp
import re
import traceback
import typing
import uuid

from flask import (jsonify, request, abort, make_response,
                   render_template_string, send_from_directory,
                   has_request_context)
import jwt
from  werkzeug.exceptions import HTTPException, HTTP_STATUS_CODES

from bv_rest.database import get_cursor

class ServicesConfig:
    @property
    def services_dir(self):
        # This base path is hardcoded as long as it is only
        # used in a container with always the same value.
        return '/bv_services'
    
    @property
    def postgres_user(self):
        return open(osp.join(self.services_dir, 'postgres_user')).read().strip()

    @property
    def postgres_password(self):
        return open(osp.join(self.services_dir, 'postgres_password')).read()


config = ServicesConfig()

def get_roles():
    token = request.headers.get('api_key')
    if token:
        public_key = open('/bv_auth/id_rsa.pub').read()
        try:
            payload = jwt.decode(token, public_key, issuer='bv_auth', algorithm='RS256')
        except:
            abort(401)
        login = payload.get('login')
        sql = 'SELECT roles FROM user_roles_cache WHERE login=%s'
        cursor.execute(sql, [login])
        if cursor.rowcount:
            return set(cursor.fetchone()[0])
        else:
            sql = 'SELECT role, given_to, inherit FROM granting'
            cur.execute(sql)
            grantings = {}
            links = {}
            for role, given_to, inherit in cur:
                grantings.setdefault(given_to, set()).add(role)
                if inherit:
                    links.setdefault(given_to, set()).add(role)
            user_role = f'${login}'
            roles = {user_role}
            roles.update(grantings.get(user_role, set()))
            new_roles = set()
            while True:
                for role in roles:
                    new_roles.add(role)
                    for linked_role in links.get(role, set()):
                        new_roles.update(grantings.get(linked_role, set()))
                if new_roles == roles:
                    break
                roles = new_roles
            sql = 'INSERT INTO user_roles_cache (login, roles) VALUES (%s, %s)'
            cursor.execute(sql, [login, list(roles)])
            return roles
    abort(401)

class RestAPI:
    class Path:
        def __init__(self, path):
            self.path = path
            self.path_parameters = [i.group(0)[1:-1].split(':',1)[-1] 
                                    for i in re.finditer(r'<[^>]*>', self.path)]
            
            self.get = None
            self.post = None
            self.put = None
            self.delete = None
            
    class Operation:
        def __init__(self, api, path):
            self.api = api
            self.path = path
            self.param_in_body = False
            
        def __call__(self, function=None, 
                     param_in_body=False):
            if function is None:
                self.param_in_body = param_in_body
                return self
            else:
                self.id = str(uuid.uuid4())
                http_method = function.__name__
                if http_method not in ('get', 'post', 'put', 'delete'):
                    raise NameError('%s is not a valid function name to guess HTTP method, use get, post, put or delete.' % http_method)        
                if getattr(self.path, http_method) is not None:
                    raise NameError('A function is already defined for HTTP method %s on route %s' % (method, self.path))
                
                function.param_in_body = self.param_in_body
                setattr(self.path, http_method, function)
                
                argspec = inspect.getfullargspec(function)
                json_args = [i for i in argspec.args if i not in self.path.path_parameters]
                function.json_args = json_args
                function.path_parameters = self.path.path_parameters
                
                @self.api.flask_app.route(self.path.path, 
                                          endpoint=self.id,
                                          methods=[http_method.upper(), 'OPTIONS'])
                @wraps(function)
                def wrapper(**kwargs):
                    if request.method == 'OPTIONS':
                        # Handle options is necessary to allow web pages that
                        # are not on the same server (e.g. local pages) to use 
                        # the API. See 
                        # https://www.html5rocks.com/en/tutorials/cors/
                        acrh = request.headers['Access-Control-Request-Headers']
                        response = make_response('', 200)
                        response.headers['Access-Control-Allow-Origin'] = '*'
                        response.headers['Access-Control-Allow-Methods'] = 'GET,PUT,POST'
                        response.headers['Access-Control-Allow-Headers'] = acrh
                    else:
                        try:
                            response = None
                            args = ()
                            try:
                                if function.param_in_body:
                                    args = (request.get_json(force=True),)
                                elif function.json_args:
                                    kwargs.update(request.get_json(force=True))
                            except Exception as e:
                                error = {
                                    'message': 'Request does not contain valid JSON',
                                }
                                response = make_response(error, 400)
                            if response is None:
                                result = function(*args, **kwargs)
                                try:
                                    response = jsonify(result)
                                except Exception as e:
                                    error = {
                                        'message': 'Value cannot be converted to JSON (%s): %s' % (str(e), repr(result)),
                                        'traceback': traceback.format_exc(),
                                    }
                                    response = make_response(error, 500)
                        except HTTPException as e:
                            error = {
                                'message': str(e),
                            }
                            response = make_response(error, e.code)
                        except Exception as e:
                            error = {
                                'message': '%s: %s' % (e.__class__.__name__, str(e)),
                                'traceback': traceback.format_exc(),
                            }
                            response = make_response(error, 500)
                    response.headers['Access-Control-Allow-Origin'] = '*'
                    return response
                
                return function

    def __init__(self, flask_app, title, description, version):
        self.flask_app = flask_app
        self.title = title
        self.description = description
        self.version = version
        self.schemas = []
        self.paths = OrderedDict()

    def schema(self, cls):
        self.schemas.append(cls)
        return cls

    def path(self, path):
        path_obj = self.paths.get(path)
        if not path_obj:
            path_obj = self.Path(path)
            self.paths[path] = path_obj
        return self.Operation(self, path_obj)
    
    def require_role(self, role):
        def decorator(function):
            function.has_security = True
            @wraps(function)
            def wrapper(*args, **kwargs):
                if role not in get_roles():
                    abort(403)
                return function(*args, **kwargs)
            return self.may_abort(401)(self.may_abort(403)(wrapper))
        return decorator
            
    def may_abort(self, http_code):
        def decorator(function):
            if http_code not in HTTP_STATUS_CODES:
                raise ValueError(f'Invalid HTTP code: {http_code}')
            codes = getattr(function, 'may_abort', [])
            codes.append(http_code)
            function.may_abort = codes
            return function
        return decorator
        
    @property
    def open_api(self):
        result = OrderedDict([
            ('openapi', '3.0.2'),
            ('info', OrderedDict([
                ('title', self.title),
                ('description', self.description),
                ('version', self.version),
            ])),
            ('components', OrderedDict([
                ('securitySchemes', OrderedDict([
                    ('api_key', OrderedDict([
                        ('type', 'apiKey'),
                        ('name', 'api_key'),
                        ('in', 'header'),
                        ('description', 'a JWT obtained with `/api_key` service'),
                    ])),
                ])),
                ('schemas', OrderedDict([
                    ('Exception', OrderedDict([
                        ('type', 'object'),
                        ('required', ['message']),
                        ('properties', OrderedDict([
                            ('message', OrderedDict([
                                ('type', 'string'),
                            ])),
                            ('traceback', OrderedDict([
                                ('type', 'string'),
                            ])),
                        ])),
                    ]))
                ])),
            ])),
        ])
        if has_request_context():
            result['servers'] = [{'url': f'{request.headers["X-Forwarded-Proto"]}://{request.headers["X-Forwarded-Host"]}{request.headers["X-Forwarded-Prefix"]}'}]
            
        for cls in self.schemas:
            if len(cls.__bases__) != 1:
                raise TypeError('Open API implementation does not support multiple inheritance')
            base = cls.__bases__[0]
            if base is object:
                base = None
            schema = OrderedDict()
            result['components']['schemas'][cls.__name__] = schema
            required = []
            for n, t in cls.__annotations__.items():
                if getattr(t, '__origin__', None) != typing.Union or \
                   type(None) not in t.__args__:
                       required.append(n)
            if base:
                schema2 = OrderedDict()
                schema['allOf'] = [OrderedDict([('$ref', '#/components/schemas/%s' % base.__name__)]),
                                   schema2]
                schema = schema2
            if required:
                schema['required'] = required
            schema['properties'] = OrderedDict()
            for n, t in cls.__annotations__.items():
                schema['properties'][n] = self.type_to_open_api(t)
    
        result['paths'] = OrderedDict()
        for path in self.paths.values():
            path_dict = OrderedDict()
            result['paths'][path.path] = path_dict
            if path.path_parameters:
                path_dict['parameters'] = []
                for parameter in path.path_parameters:
                    path_dict['parameters'].append(OrderedDict([
                        ('name', parameter),
                        ('in', 'path'),
                        ('required', True),
                        ('schema', {'type': 'string'}),
                    ]))
            for http_method in ('get', 'post', 'put', 'delete'):
                function = getattr(path, http_method, None)
                if function is not None:
                    operation = OrderedDict()
                    path_dict[http_method] = operation
                    operation['summary'] = function.__doc__
                    argspec = inspect.getfullargspec(function)
                    args = [i for i in argspec.args if i not in path.path_parameters]
                    if getattr(function, 'has_security', False):
                        operation['security'] = [OrderedDict([('api_key', [])])]
                    if args:
                        if function.param_in_body:
                            if len(args) != 1:
                                raise ValueError('param_in_body can only be used with a single parameter but several were found: %s' % ', '.join(args))
                            arg = args[0]
                            body_type = argspec.annotations.get(arg)
                            if body_type is None:
                                raise TypeError('cannot determine function return type')
                            operation['requestBody'] = OrderedDict([
                                ('description', '%s parameter' % arg),
                                ('required', True),
                                ('content', {'application/json': {'schema': self.type_to_open_api(body_type)}}),
                            ])
                        else:
                            properties = OrderedDict()
                            operation['requestBody'] = OrderedDict([
                                ('description', 'parameters'),
                                ('required', True),
                                ('content',
                                    {'application/json': 
                                        {'schema': 
                                            {'type': 'object',
                                             'properties': properties}}}),
                            ])
                            for arg in args:
                                arg_type = argspec.annotations[arg]
                                properties[arg]= self.type_to_open_api(arg_type)
                    responses = OrderedDict()
                    operation['responses'] = responses
                    for http_code in getattr(function, 'may_abort', []):
                        responses[str(http_code)] = OrderedDict([
                                ('description', HTTP_STATUS_CODES[http_code]),
                                ('content', {'application/json': {'schema': {'$ref': '#/components/schemas/Exception'}}}),
                            ])
                        
                    responses['default'] = OrderedDict([
                            ('description', 'Unexpected error'),
                            ('content', {'application/json': {'schema': {'$ref': '#/components/schemas/Exception'}}}),
                        ])
                    return_type = function.__annotations__.get('return')
                    if return_type is typing.NoReturn:
                        operation['responses']['204'] = {'description': 'Success'}
                    else:
                        operation['responses']['200'] = OrderedDict([
                            ('description', 'Success'),
                            ('content', {'application/json': {'schema': self.type_to_open_api(return_type)}}),
                        ])
        return result
    
    _type_to_open_api = {
        str: ('string', None),
        bytes: ('string', 'byte'),
        int: ('integer', 'int64'),
        float: ('number', 'double'),
        datetime.date: ('string', 'date'),
        datetime.datetime: ('string', 'date-time'),
    }

    def type_to_open_api(self, type_def):
        result = OrderedDict()
        if getattr(type_def, '__origin__', None) is typing.Union:
            # Replace Optional[T] by T in type_def
            if len(type_def.__args__) == 2 and type_def.__args__[1] is type(None):
                type_def = type_def.__args__[0]
        oa_type_format = self._type_to_open_api.get(type_def)
        if oa_type_format:
            t, f = oa_type_format
            result['type'] = t
            if f:
                result['format'] = f
        else:
            typing_type = getattr(type_def, '__origin__', None)
            # The following line is working with Python 3.7. Python 3.6 does not return list but typing.List
            if typing_type is list:
                if len(type_def.__args__) > 1:
                    raise TypeError('Open API implementation does not support list with several item types: %s' % str(type_def))
                result['type'] = 'array'
                result['items'] = self.type_to_open_api(type_def.__args__[0])
            elif isinstance(type_def, type):
                result['$ref'] = '#/components/schemas/%s' % type_def.__name__
            else:
                raise TypeError('Open API implementation does not support this object type: %s' % str(type_def))
        return result

def init_api(api):
    @api.path('/api')
    def get() -> str:
        'Return an OpenAPI 3.0.2 specification for this API'
        return api.open_api
    
    @api.flask_app.route('/')
    def swagger_ui():
        return render_template_string(open(osp.join(osp.dirname(__file__), 'swagger-ui.html')).read())

    @api.flask_app.route('/api/<path:filename>')
    def swagger_ui_files(filename):
        return send_from_directory(osp.join(osp.dirname(__file__), 'swagger-ui'),
                                   filename)

import datetime
from collections import OrderedDict
from functools import partial
import inspect
import re
import traceback
import typing
import uuid

from flask import jsonify, request
from  werkzeug.exceptions import HTTPException, HTTP_STATUS_CODES

class ServicesConfig:
    @property
    def services_dir(self):
        # This base path is hardcoded as long as it is only
        # used in a container with always the same value.
        return '/bv_services'
    
config = ServicesConfig()

class RestAPI:
    class Path:
        def __init__(self, path):
            self.path = path
            self.path_parameters = [i.group(0)[1:-1] for i in re.finditer(r'\{\w*\}', self.path)]
            
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
                
                @self.api.flask_app.route(self.path.path, 
                                          endpoint=self.id,
                                          methods=[http_method.upper()])
                def f():
                    try:
                        args = ()
                        kwargs = {}
                        if function.param_in_body:
                            args = (request.get_json(force=True),)
                        elif function.json_args:
                            kwargs = request.get_json(force=True)
                        result = function(*args, **kwargs)
                        try:
                            json_result = jsonify(result)
                        except Exception as e:
                            error = {
                                'message': 'Value cannot be converted to JSON (%s): %s' % (str(e), repr(result)),
                                'traceback': traceback.format_exc(),
                            }
                            return error, 500
                        return json_result
                    except HTTPException:
                        raise
                    except Exception as e:
                        error = {
                            'message': '%s: %s' % (e.__class__.__name__, str(e)),
                            'traceback': traceback.format_exc(),
                        }
                        return error, 500 
                
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
            return function
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
                                ('content', {'text/html': {'schema': {'type': 'string'}}}),
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
    

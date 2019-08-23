from setuptools import setup, find_packages

setup(
    name='bv_rest',
    version='0.0.1',
    description='OpenAPI compatible JSON/REST API definition with flask',
    classifiers=[
        'Programming Language :: Python',
        "Programming Language :: Python :: 3",
        'Framework :: Flask',
        'Topic :: Internet :: WWW/HTTP',
        'Topic :: Internet :: WWW/HTTP :: WSGI :: Application',
    ],
    keywords='rest web flask',
    packages=['bv_rest'],
    install_requires=[
        'flask >= 1.0',
        'psycopg2-binary >= 2.7',
        'gunicorn',
        'decorator',
    ],
)

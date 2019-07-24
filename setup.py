try:
    from setuptools import setup
    from setuptools import find_packages
except ImportError:
    from distutils.core import setup

with open('requirements.txt') as f:
    install_requires = f.read().strip().split("\n")

config = {
    'description': 'Tornado-based HTTP/2 Client',

    'url': 'https://github.com/ContextLogic/h2tornado',
    'author': 'Andrew Huang',
    'author_email': 'andrew@wish.com',
    'license': 'MIT',
     'classifiers': [
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 2.7',
    ],
    'version': '0.0.2',
    'packages': ['h2tornado'],
    'scripts': [],
    'name': 'h2tornado',
    # TODO: unify with requirements.txt
    'install_requires': install_requires,
}

setup(**config)

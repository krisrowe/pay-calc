from setuptools import setup, find_packages
import re

# Read version from paycalc/__init__.py
with open('paycalc/__init__.py') as f:
    version = re.search(r'^__version__ = ["\']([^"\']+)["\']', f.read(), re.MULTILINE).group(1)

setup(
    name='paycalc',
    version=version,
    packages=find_packages(),
    install_requires=[
        'PyPDF2>=3.0.0',
        'PyYAML>=6.0',
        'click>=8.0',
    ],
    entry_points={
        'console_scripts': [
            'pay-calc=paycalc.cli.__main__:main',
        ],
    },
    author='Personal',
    description='Personal pay and tax projection tools.',
    python_requires='>=3.8',
)

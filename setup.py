import os
import setuptools

from cx_Oracle import version

setuptools.setup(
    name='cx_Oracle',
    version=version,
    description='cx_Oracle on ctypes',
    license='BSD',
    author='Leandro Lameiro',
    author_email='lameiro@gmail.com',
    url='https://github.com/lameiro/cx_oracle_on_ctypes',
    packages=setuptools.find_packages(),
    include_package_data=True,
    classifiers=[
        'Development Status :: 4 - Beta',
        'License :: OSI Approved :: Apache Software License',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Programming Language :: Python :: 2',
        'Programming Language :: Python :: 2.6',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.2',
        'Programming Language :: Python :: 3.3',
    ],
    py_modules=[])

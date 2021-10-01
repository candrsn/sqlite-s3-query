import setuptools


def long_description():
    with open('README.md', 'r') as file:
        return file.read()

def get_version():
    from sqlite_s3_query import version
    return version

version = get_version()

setuptools.setup(
    name='sqlite-s3-query',
    version=version,
    author='Michal Charemza',
    author_email='michal@charemza.name',
    description='Python context manager to query a SQLite file stored on S3',
    long_description=long_description(),
    long_description_content_type='text/markdown',
    url='https://github.com/michalc/sqlite-s3-query',
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: MIT License',
        'Topic :: Database',
    ],
    python_requires='>=3.6.0',
    install_requires=[
        'httpx>=0.18.2',
        'boto3>=1.18.51'
    ],
    py_modules=[
        'sqlite_s3_query',
    ],
)

from setuptools import setup

setup(
    name='beets-guessmedia',
    version='0.0.1',
    description='beets plugin to guess media type',
    long_description=open('README.md').read(),
    author='8h2a',
    author_email='0x000000000000002a@gmail.com',
    url='https://github.com/8h2a/beets-guessmedia',
    license='MIT',
    platforms='ALL',

    packages=['beetsplug'],

    install_requires=[
        'beets>=1.4.7',
    ],
)

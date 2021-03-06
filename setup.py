#!/usr/bin/env python

from setuptools import setup, find_packages
import txu1
import os

pkg_root = os.path.dirname(__file__)

# Error-handling here is to allow package to be built w/o README included
try: readme = open(os.path.join(pkg_root, 'README.txt')).read()
except IOError: readme = ''

setup(

	name = 'txu1', # just trying to mimic tx* naming convention
	version = txu1.__version__,
	author = 'Mike Kazantsev',
	author_email = 'mk.fraggod@gmail.com',
	license = 'WTFPL',
	keywords = [
		'ubuntu one', 'u1', 'ubuntu', 'one', 'twisted', 'sso',
		'async', 'api', 'rest', 'json', 'storage', 'storage provider', 'file hosting' ],
	url = 'https://github.com/mk-fg/txu1',

	description = 'Twisted-based async'
		' interface for Ubuntu One Files Cloud REST API v1',
	long_description = readme,

	classifiers = [
		'Development Status :: 4 - Beta',
		'Environment :: Plugins',
		'Framework :: Twisted',
		'Intended Audience :: Developers',
		'Intended Audience :: Information Technology',
		'License :: OSI Approved',
		'Operating System :: OS Independent',
		'Programming Language :: Python',
		'Programming Language :: Python :: 2.7',
		'Programming Language :: Python :: 2 :: Only',
		'Topic :: Internet',
		'Topic :: System :: Archiving',
		'Topic :: System :: Filesystems' ],

	install_requires = ['Twisted >= 12.2.0', 'oauth2'],

	packages = find_packages(),
	include_package_data = True,
	package_data = {'': ['README.txt']},
	exclude_package_data = {'': ['README.*']},

	entry_points=dict(console_scripts=['u1-cli = txu1.cli_tool:main']) )

#!/usr/bin/env python
#-*- coding: utf-8 -*-
from __future__ import print_function

import itertools as it, operator as op, functools as ft
from os.path import dirname, basename, exists, isdir, join, abspath
import os, sys, getpass

from twisted.internet import defer, reactor
from twisted.python.failure import Failure

try:
	from txu1.api_v1 import txU1
except ImportError:
	# Make sure it works from a checkout
	if isdir(join(dirname(__file__), 'txu1')) \
		and exists(join(dirname(__file__), 'setup.py')):
		sys.path.insert(0, dirname(__file__))
		from txu1.api_v1 import txU1
	else:
		from api_v1 import txU1


def main(argv=None):
	import argparse
	parser = argparse.ArgumentParser(description='Ubuntu One API tool.')

	cmds = parser.add_subparsers(title='Supported operations')

	def add_command(name, **kwz):
		cmd = cmds.add_parser(name, **kwz)
		cmd.set_defaults(call=name)
		return cmd

	cmd = add_command('auth',
		help='Create OAuth token for Ubuntu SSO account.'
			' Prints acquired credentials to stdout by default.'
			' See: https://login.ubuntu.com/+applications')
	cmd.add_argument('-c', '--config', action='store_true',
		help='Output in YAML configuration format, supported by txU1Persistent.'
			' Requires "yaml" python module available.')
	cmd.add_argument('-o', '--output', metavar='file',
		help='Specify a file to output credentials to (instead of stdout).')
	cmd.add_argument('-n', '--token-name', metavar='name',
		help='Token name to use (will be visible on "applications" page).'
			' Recommended (by docs) format is "{hostname} [{app_name}]".'
			' "Ubuntu One @" will be automatically prepended to it, if necessary (required by API).')

	optz = parser.parse_args(argv if argv is not None else sys.argv[1:])

	if optz.call == 'auth':
		@defer.inlineCallbacks
		def action():
			if optz.config: import yaml
			if optz.token_name and not optz.token_name.startswith('Ubuntu One @'):
				optz.token_name = 'Ubuntu One @ {}'.format(optz.token_name.lstrip())
			api = txU1()
			print('Requesting new OAuth credentials (token name: {})'.format(optz.token_name))
			email = raw_input('  Ubuntu Account Email: ').strip()
			password = getpass.getpass('  Ubuntu Account Password: ').strip()
			assert email and password,\
				'Need both email and password to get credentials'
			auth = yield api.auth_create_token(email, password, token_name=optz.token_name)
			auth = dict(auth_consumer=auth[0], auth_token=auth[1])
			dst = sys.stdout if not optz.output else open(optz.output, 'w')
			if optz.config: yaml.safe_dump(auth, dst, default_flow_style=False)
			else:
				p = ft.partial(print, file=dst)
				p('consumer_key: {}'.format(auth['auth_consumer'][0]))
				p('consumer_secret: {}'.format(auth['auth_consumer'][1]))
				p('token: {}'.format(auth['auth_token'][0]))
				p('token_secret: {}'.format(auth['auth_token'][1]))

	else:
		parser.error('Unrecognized command: {}'.format(optz.call))


	def done(res):
		if reactor.running: reactor.stop()
		return res

	reactor.callWhenRunning(lambda: action().addBoth(done))
	reactor.run()


if __name__ == '__main__': sys.exit(main())

txu1
----------------------------------------

Twisted-based async interface for [Ubuntu One](https://one.ubuntu.com) [Files
Cloud REST API v1](https://one.ubuntu.com/developer/files/store_files/cloud).

 * [API docs](https://one.ubuntu.com/developer/files/store_files/cloud)
 * [API Auth docs](https://one.ubuntu.com/developer/account_admin/auth/index)

Implemented from scratch on top of Twisted and
[oauth2](https://pypi.python.org/pypi/oauth2/) (name might be confusing - it's
actually OAuth 1.0 helper), does not require any ubuntu-specific libs, daemons
or modules.



Usage Example
----------------------------------------

Note that email/password credentials are only needed to get the OAuth 1.0 token
(which can be revoked through [ubuntu single sign on
interface](https://login.ubuntu.com/+applications)) once, see
[docs](https://one.ubuntu.com/developer/account_admin/auth/otherplatforms) for
more details.

More comprehensive docs are always welcome!

	from twisted.internet import defer, reactor
	from twisted.python import log
	from txu1 import txU1API, DoesNotExist

	@defer.inlineCallbacks
	def do_stuff():
		api = txU1API(debug_requests=True)

		try:
			api.auth_consumer, api.auth_token =\
				(open(n).read().splitlines() for n in ['u1_consumer', 'u1_token'])
		except (OSError, IOError):
			log.info('Getting new OAuth credentials')

			# Query credentials from terminal
			email = raw_input('U1 Email: ').strip()
			password = raw_input('U1 Password: ').strip()
			assert email and password, (email, password)

			auth = yield api.auth_create_token(email, password)
			open('u1_consumer', 'w').write('{}\n{}\n'.format(*api.auth_consumer))
			open('u1_token', 'w').write('{}\n{}\n'.format(*api.auth_token))
			log.info('Auth data acquired: {}'.format(auth))

		log.info('Storage info: {}'.format((yield api.info_storage())))
		log.info('Public files: {}'.format((yield api.info_public_files())))

		vol_list = yield api.volume_info(type_filter='udf')
		vol_count = len(vol_list)
		log.info('UDF volumes: {}'.format(vol_list))

		try: vol_info = yield api.volume_info('~/test')
		except DoesNotExist:
			vol_info = yield api.volume_create('~/test')
			vol_count += 1
		log.info('Using volume: {}'.format(vol_info))
		if vols_count > 1: api.default_volume = '~/test'

		try: log.info('dir info: {}'.format((yield api.node_info('/a/b/c', children=True))))
		except DoesNotExist: log.info('mkdir: {}'.format((yield api.node_mkdir('/a/b/c'))))

		contents = 'somecontents'
		log.info('put: {}'.format(
			(yield api.file_put_into('/a/b/c', name='test_file', data=contents)) ))
		log.info('put_magic: {}'.format(
			(yield api.file_put_magic('/a/b/c/test_file2', data=contents)) ))

		log.info('get: {}'.format((yield api.file_get('/a/b/c/test_file'))))

		yield api.node_delete('/a/b/c/test_file2')
		yield api.node_delete('/a/b/c/test_file')
		yield api.node_delete('/a/b/c')
		yield api.volume_delete('~/test')

		log.info('Done')

	do_stuff().addBoth(lambda ignored: reactor.stop())
	reactor.run()



Installation
----------------------------------------

It's a regular package for Python 2.7 (not 3.X).

Using [pip](http://pip-installer.org/) is the best way:

	% pip install txu1

If you don't have it, use:

	% easy_install pip
	% pip install txu1

Alternatively ([see
also](http://www.pip-installer.org/en/latest/installing.html)):

	% curl https://raw.github.com/pypa/pip/master/contrib/get-pip.py | python
	% pip install txu1

Or, if you absolutely must:

	% easy_install txu1

But, you really shouldn't do that.

Current-git version can be installed like this:

	% pip install 'git+https://github.com/mk-fg/txu1.git#egg=txu1'

Note that to install stuff in system-wide PATH and site-packages, elevated
privileges are often required.
Use "install --user",
[~/.pydistutils.cfg](http://docs.python.org/install/index.html#distutils-configuration-files)
or [virtualenv](http://pypi.python.org/pypi/virtualenv) to do unprivileged
installs into custom paths.


### Requirements

* [Python 2.7 (not 3.X)](http://python.org)

* [Twisted](http://twistedmatrix.com) (core, web, at least 12.2.0)

* [oauth2](https://pypi.python.org/pypi/oauth2/)

#-*- coding: utf-8 -*-

import itertools as it, operator as op, functools as ft
from mimetypes import guess_type
from collections import Mapping, OrderedDict
import os, sys, io, re, types, logging
import urllib, hashlib

from OpenSSL import crypto
from zope.interface import implements

import oauth2 as oauth

from twisted.web.iweb import IBodyProducer, UNKNOWN_LENGTH
from twisted.web.http_headers import Headers
from twisted.web import http
from twisted.python.failure import Failure
from twisted.internet import defer, reactor, ssl, task, protocol
from twisted.internet.error import TimeoutError

from twisted.web.client import Agent, RedirectAgent,\
	HTTPConnectionPool, HTTP11ClientProtocol, ContentDecoderAgent, GzipDecoder
from twisted.web.client import ResponseDone,\
	ResponseFailed, RequestNotSent, RequestTransmissionFailed

try: # doesn't seem to be a part of public api
	from twisted.web._newclient import RequestGenerationFailed
except ImportError: # won't be handled
	class RequestGenerationFailed(Exception): pass


try: import anyjson as json
except ImportError:
	try: import simplejson as json
	except ImportError: import json


from twisted.python import log as twisted_log

class log(object): pass # proxy object, emulating python logger
for lvl in 'debug', 'info', ('warning', 'warn'), 'error', ('critical', 'fatal'):
	lvl, func = lvl if isinstance(lvl, tuple) else (lvl, lvl)
	setattr(log, func, staticmethod(ft.partial(
		twisted_log.msg, logLevel=logging.getLevelName(lvl.upper()) )))

def log_web_failure(err, err_lid=''):
	'Try to print meaningful info about wrapped twisted.web exceptions.'
	if err_lid and not err_lid.endswith(' '): err_lid += ' '
	try: err.value.reasons # multiple levels of fail
	except AttributeError: pass
	else: err = err.value
	if hasattr(err, 'reasons'):
		for err in err.reasons:
			lid = '  '
			if isinstance(err, Failure):
				log.error('{}{} {}: {}'.format(err_lid, lid, err.type, err.getErrorMessage()))
				for line in err.getTraceback().splitlines():
					log.error('{}{}   {}'.format(err_lid, lid, line))
			else: log.error('{}{} {}: {}'.format(err_lid, lid, type(err), err))

def force_bytes(bytes_or_unicode, encoding='utf-8', errors='backslashreplace'):
	'Convert passed string type to bytes, if necessary.'
	if isinstance(bytes_or_unicode, bytes): return bytes_or_unicode
	return bytes_or_unicode.encode(encoding, errors)



class U1InteractionError(Exception): pass

class ProtocolError(U1InteractionError):
	def __init__(self, code, msg):
		super(ProtocolError, self).__init__(code, msg)
		self.code = code

class DoesNotExist(U1InteractionError): pass

class AuthenticationError(U1InteractionError): pass




class UnderlyingProtocolError(ProtocolError):
	'Raised for e.g. ResponseFailed non-HTTP errors from HTTP client.'

	def __init__(self, err):
		# Set http-503, to allow handling of it similar way for http-oriented code
		super(UnderlyingProtocolError, self)\
			.__init__(http.SERVICE_UNAVAILABLE, err.message)
		self.error = err



class DataReceiver(protocol.Protocol):

	def __init__(self, done, timer=None, allow_loss=False):
		self.done, self.timer, self.data = done, timer, list()
		self.allow_loss = allow_loss

	def dataReceived(self, chunk):
		if self.timer:
			if not self.data: self.timer.state_next('res_body') # first chunk
			else:
				try: self.timer.timeout_reset()
				except self.timer.TooLate as err:
					self.done.errback(err)
					self.timer = self.data = None
		if self.data is not None: self.data.append(chunk)

	def connectionLost(self, reason):
		if self.timer: self.timer.state_next()
		if not isinstance(reason.value, ResponseDone)\
				and not (self.allow_loss and isinstance(reason.value, http.PotentialDataLoss)):
			self.done.callback(reason)
		elif not self.done.called: # might errback due to timer
			self.done.callback(
				b''.join(self.data) if self.data is not None else b'' )



class FileBodyProducer(object):
	implements(IBodyProducer)

	_task = None

	#: Single read/write size
	chunk_size = 64 * 2**10 # 64 KiB

	def __init__(self, src, timer=None):
		self.src, self.timer = src, timer

		# Set length, if possible
		try: src.seek, src.tell
		except AttributeError: self.length = UNKNOWN_LENGTH
		else:
			src.seek(0, os.SEEK_END)
			self.length = src.tell()

	@defer.inlineCallbacks
	def upload_file(self, src, dst):
		src.seek(0)
		while True:
			if self.timer:
				try: self.timer.timeout_reset()
				except self.timer.TooLate as err:
					self.timer = None
					break
			chunk = src.read(self.chunk_size)
			if not chunk: break
			yield dst.write(chunk)

	@defer.inlineCallbacks
	def send(self, dst):
		res = yield self.upload_file(self.src, dst)
		if self.timer: self.timer.state_next()
		defer.returnValue(res)

	def startProducing(self, dst):
		if self.timer: self.timer.state_next('req_body')
		if not self._task: self._task = self.send(dst)
		return self._task

	def resumeProducing(self):
		if not self._task: return
		self._task.unpause()

	def pauseProducing(self):
		if not self._task: return
		self._task.pause()

	def stopProducing(self):
		if not self._task: return
		self._task.cancel()
		self._task = None


class MultipartDataSender(FileBodyProducer):

	def __init__(self, fields, boundary, timer=None):
		self.fields, self.boundary, self.timer = fields, boundary, timer
		self.length = UNKNOWN_LENGTH

	@defer.inlineCallbacks
	def calculate_length(self, dst=None):
		if not dst: self.length = yield self.send()
		else:
			pos = dst.tell()
			yield self.send(dst)
			self.length = dst.tell() - pos

	@defer.inlineCallbacks
	def send(self, dst=None):
		dry_run = not dst # allows to calculate length without reading files
		if dry_run: dst, dst_ext = io.BytesIO(), 0

		for name, data in self.fields.viewitems():
			dst.write(b'--{}\r\n'.format(self.boundary))

			ct = None
			if isinstance(data, tuple):
				fn, data = data
				ct = guess_type(fn)[0] or b'application/octet-stream'
				dst.write(
					b'Content-Disposition: form-data;'
					b' name="{}"; filename="{}"\r\n'.format(name, fn) )
			else:
				ct = b'text/plain'
				dst.write( b'Content-Disposition:'
					b' form-data; name="{}"\r\n'.format(name) )
			dst.write(b'Content-Type: {}\r\n\r\n'.format(ct) if ct else b'\r\n')

			if isinstance(data, types.StringTypes): dst.write(data)
			elif not dry_run: yield self.upload_file(data, dst)
			else:
				data.seek(0, os.SEEK_END)
				dst_ext += data.tell()
			dst.write(b'\r\n')

		dst.write(b'--{}--\r\n'.format(self.boundary))

		if dry_run: defer.returnValue(dst_ext + len(dst.getvalue()))
		else:
			self._task = None
			if self.timer: self.timer.state_next()



class TLSContextFactory(ssl.CertificateOptions):

	isClient = 1

	def __init__(self, ca_certs_files):
		ca_certs = dict()

		for ca_certs_file in ( [ca_certs_files]
				if isinstance(ca_certs_files, types.StringTypes) else ca_certs_files ):
			with open(ca_certs_file) as ca_certs_file:
				ca_certs_file = ca_certs_file.read()
			for cert in re.findall( r'(-----BEGIN CERTIFICATE-----'
					r'.*?-----END CERTIFICATE-----)', ca_certs_file, re.DOTALL ):
				cert = crypto.load_certificate(crypto.FILETYPE_PEM, cert)
				ca_certs[cert.digest('sha1')] = cert

		super(TLSContextFactory, self).__init__(verify=True, caCerts=ca_certs.values())

	def getContext(self, hostname, port):
		return super(TLSContextFactory, self).getContext()


class QuietHTTP11ClientFactory(protocol.Factory):

	noisy = False
	protocol = HTTP11ClientProtocol

	def __init__(self, quiescentCallback):
		self._quiescentCallback = quiescentCallback

	def buildProtocol(self, addr):
		return self.protocol(self._quiescentCallback)


class QuietHTTPConnectionPool(HTTPConnectionPool):

	_factory = QuietHTTP11ClientFactory

	def __init__(self, reactor, persistent=True, debug_requests=False, **pool_kwz):
		super(QuietHTTPConnectionPool, self).__init__(reactor, persistent=persistent)
		for k, v in pool_kwz.viewitems():
			getattr(self, k) # to somewhat protect against typos
			setattr(self, k, v)



class HTTPTimeout(defer.Deferred, object):

	'''Deferred that will errback if timeout_reset() won't be called in time.
		What "in time" means depends on current state and state_timeouts.
		States can be switched by state_next() method.
		Callback is invoked when the last state is passed or on state_finished() call.'''

	class ActivityTimeout(Exception): pass
	class TooLate(Exception): pass

	_state = _timeout = None
	state_timeouts = OrderedDict([ ('req_headers', 60),
		('req_body', 20), ('res_headers', 20), ('res_body', 20), ('res_end', 10) ])

	def __init__(self, timeouts=None, **state_timeouts):
		if timeouts:
			assert not state_timeouts
			self.state_timeouts = timeouts
		elif state_timeouts:
			for k, v in state_timeouts.viewitems():
				assert k in self.state_timeouts, k
				self.state_timeouts[k] = v
		super(HTTPTimeout, self).__init__()
		self._state = next(iter(self.state_timeouts))
		self.timeout_reset()

	def state_next(self, state=None):
		if not state: # advance in order
			states = iter(self.state_timeouts)
			next(it.dropwhile(lambda k: k != self._state, states))
			try: self._state = next(states)
			except StopIteration: self.state_finished()
		else: self._state = state
		self.timeout_reset()

	def state_finished(self):
		if self._timeout.active(): self._timeout.cancel()
		if not self.called: self.callback(None)

	def timeout_reset(self):
		timeout = self.state_timeouts[self._state]
		if not self._timeout:
			self._timeout = reactor.callLater( timeout,
				lambda: self.errback(self.ActivityTimeout(
					self._state, self.state_timeouts[self._state] )) )
		elif not self._timeout.active(): raise self.TooLate()
		self._timeout.reset(timeout)



@defer.inlineCallbacks
def first_result(*deferreds):
	try:
		res, idx = yield defer.DeferredList(
			deferreds, fireOnOneCallback=True, fireOnOneErrback=True )
	except defer.FirstError as err: err.subFailure.raiseException()
	defer.returnValue(res)

def _dump_trunc(v, trunc_len=100):
	if isinstance(v, Mapping):
		return dict((k, _dump_trunc(v)) for k,v in v.iteritems())
	elif isinstance(v, (list, tuple)): return [_dump_trunc(v) for v in v]
	elif not isinstance(v, types.StringTypes): v = repr(v)
	if len(v) > trunc_len: v = v[:trunc_len] + '...'
	return v

def join(*path_nodes):
	path = ''
	for node in path_nodes:
		if not node: continue
		if not path.endswith('/'): path += '/'
		path += node.lstrip('/')
	return path


class txU1API(object):
	'U1 API client.'

	# api_url_content is a temporary caveat, described in API docs:
	#  https://one.ubuntu.com/developer/files/store_files/cloud
	api_url_meta = 'https://one.ubuntu.com/api/file_storage/v1'
	api_url_content = 'https://files.one.ubuntu.com'

	# Auth tunables
	auth_url_login = 'https://login.ubuntu.com/api/1.0/authentications'
	auth_url_token = 'https://one.ubuntu.com/oauth/sso-finished-so-get-tokens/'
	auth_token_name = 'Ubuntu One @ {hostname} [txu1]' # hostname=uname()

	# Auth credentials
	# Must be either set or acquired (generally once) via auth_create_token method
	auth_consumer = auth_token = None

	# Options to twisted.web.client.HTTPConnectionPool
	request_pool_options = dict(
		persistent = True,
		maxPersistentPerHost = 10,
		cachedConnectionTimeout = 600,
		retryAutomatically = True )

	# These are timeouts between individual read/write ops
	# Missing keys will have default values (from HTTPTimeout.state_timeouts)
	request_io_timeouts = dict( req_headers=60,
		req_body=20, res_headers=20, res_body=20, res_end=10 )

	# Path string or list of strings
	ca_certs_files = b'/etc/ssl/certs/ca-certificates.crt'

	# Dump HTTP request data in debug log (might contain all sorts of auth tokens!)
	debug_requests = False


	def __init__(self, **config):
		'Initialize API wrapper class with specified properties set.'
		for k, v in config.viewitems():
			try: getattr(self, k)
			except AttributeError:
				raise AttributeError('Unrecognized configuration key: {}'.format(k))
			setattr(self, k, v)
		pool = self.request_pool = QuietHTTPConnectionPool( reactor,
				debug_requests=self.debug_requests, **self.request_pool_options )
		self.request_agent = ContentDecoderAgent(RedirectAgent(Agent(
			reactor, TLSContextFactory(self.ca_certs_files), pool=pool )), [('gzip', GzipDecoder)])
		self.oauth = dict() # oauth object cache


	@defer.inlineCallbacks
	def request( self, url, method='get',
			decode=None, encode=None, data=None,
			headers=None, raise_for=dict(), queue_lines=None ):
		'''Make HTTP(S) request.
			headers can be either dict or callable(url, method, body, mimetype or None).
			decode (response body) = None | json.
			encode (data) = None | json | form | files.'''
		if self.debug_requests:
			url_debug = _dump_trunc(url)
			log.debug('HTTP request: {} {} (h: {}, enc: {}, dec: {}, data: {!r})'.format(
				method, url_debug, headers, encode, decode, _dump_trunc(data) ))

		timeout = HTTPTimeout(**self.request_io_timeouts)
		req_headers = {'User-Agent': 'txU1'}

		if data is not None:
			if encode == 'files':
				boundary = os.urandom(16).encode('hex')
				req_headers['Content-Type'] = 'multipart/form-data; boundary={}'.format(boundary)
				data = MultipartDataSender(data, boundary)
				if not callable(headers): yield data.calculate_length()
				else:
					body = io.BytesIO()
					yield data.calculate_length(body)
					headers = headers(url, method, body.getvalue(), 'multipart/form-data')
			else:
				if encode is None:
					if isinstance(data, types.StringTypes): data = io.BytesIO(data)
				elif encode == 'form':
					req_headers['Content-Type'] = 'application/x-www-form-urlencoded'
					data = io.BytesIO(urllib.urlencode(data))
				elif encode == 'json':
					req_headers['Content-Type'] = 'application/json'
					data = io.BytesIO(json.dumps(data))
				else: raise ValueError('Unknown request encoding: {}'.format(encode))
				if callable(headers):
					data.seek(0)
					headers = headers(url, method, data.read(), req_headers.get('Content-Type'))
				data = FileBodyProducer(data)
		elif callable(headers): headers = headers(url, method, '', None)

		# Passed "headers" should be allowed to be modified by body_hash()
		for k, v in (headers or dict()).viewitems():
			req_headers[force_bytes(k)] = force_bytes(v)
		url, method = it.imap(force_bytes, [url, method.lower()])

		if decode == 'json': req_headers.setdefault('Accept', 'application/json')
		elif decode is not None:
			raise ValueError('Unknown response decoding method: {}'.format(decode))

		res_deferred = first_result( timeout,
			self.request_agent.request( method.upper(), url,
				Headers(dict((k,[v]) for k,v in req_headers.viewitems())), data ) )
		code = res_body = None
		try:
			res = yield res_deferred
			code = res.code
			if code == http.NO_CONTENT: defer.returnValue(None)
			if code not in [http.OK, http.CREATED]:
				if self.debug_requests:
					res_body = defer.Deferred()
					res.deliverBody(DataReceiver(res_body, timer=timeout, allow_loss=True))
					res_body = yield first_result(timeout, res_body)
					log.debug('HTTP error response body: {!r}'.format(res_body))
				raise ProtocolError(code, res.phrase)

			res_body = defer.Deferred()
			res.deliverBody(DataReceiver(res_body, timer=timeout))
			res_body = yield first_result(timeout, res_body)

			if self.debug_requests:
				log.debug( 'HTTP request done ({} {}): {} {} {}, body_len: {}'\
					.format(method, url_debug, code, res.phrase, res.version, len(res_body)) )
			defer.returnValue(json.loads(res_body) if decode is not None else res_body)

		except ( timeout.ActivityTimeout, TimeoutError,
				ResponseFailed, RequestNotSent, RequestTransmissionFailed ) as err:
			if isinstance(err, timeout.ActivityTimeout):
				if not res_deferred.called: res_deferred.cancel()
				if res_body and not res_body.called: res_body.cancel()
			if self.debug_requests:
				log.debug(
					'HTTP transport (underlying protocol) error ({} {}): {}'\
					.format(method, url_debug, err.message or repr(err.args)) )
			raise UnderlyingProtocolError(err)

		except ProtocolError as err:
			if self.debug_requests:
				log.debug(
					'HTTP request handling error ({} {}, code: {}): {}'\
					.format(method, url_debug, code, err.message) )
			raise raise_for.get(code, ProtocolError)(code, err.message)

		except RequestGenerationFailed as err:
			err[0][0].raiseException()

		finally: timeout.state_finished()


	def _auth_headers( self, url, method='get', body='', mime=None,
			base=dict(), sign_method=oauth.SignatureMethod_HMAC_SHA1() ):
		cache_key = tuple(self.auth_consumer) + tuple(self.auth_token)
		try: c, t = self.oauth[cache_key]
		except KeyError:
			c, t = oauth.Consumer(*self.auth_consumer),\
				oauth.Token(*self.auth_token)
			self.oauth.clear()
			self.oauth[cache_key] = c, t
		req = oauth.Request.from_consumer_and_token(
			c, token=t, body=body, http_url=url,
			http_method=method.upper(),
			is_form_encoded=(mime == 'application/x-www-form-urlencoded') )
		req.sign_request(sign_method, c, t)
		return dict(it.chain(base.viewitems(), req.to_header().viewitems()))

	def _api_url(self, path, query=dict(), content=False, pass_empty_values=False):
		query = query.copy()
		if not pass_empty_values:
			for k, v in query.viewitems():
				if not v:
					raise ValueError('Empty key {!r} for API call (path: {})'.format(k, path))
		if path and not path.startswith('/'): path = '/' + path
		if query: path += '?{}'.format(urllib.urlencode(query))
		return (self.api_url_meta if not content else self.api_url_content) + path

	def __call__( self, url='', query=dict(),
			query_filter=True, content=False, method='get', **request_kwz ):
		'''Make an arbitrary call to the API.
			Shouldn't be used directly under most circumstances.'''
		if query_filter:
			query = dict( (k, v) for k, v in
				query.viewitems() if v is not None )
		api_url = self._api_url(url, query, content=content)
		request_kwz.setdefault('decode', 'json')

		def _auth_headers(url, method, body, mime, base):
			url = url.split(' ', 1)[0] # XXX: no idea why API does that, bug?
			return self._auth_headers(url, method, body, mime, base=base)
		request_kwz['headers'] = ft.partial(
			_auth_headers, base=request_kwz.get('headers', dict()) )

		return self.request(api_url, method=method, **request_kwz)


	@defer.inlineCallbacks
	def auth_create_token(self, email, password):
		# Using email/password, get OAuth consumer/token
		res = yield self.request(
			'{}?{}'.format(self.auth_url_login, urllib.urlencode({ 'ws.op': 'authenticate',
				'token_name': self.auth_token_name.format(hostname=os.uname()[1]) })),
			headers={'Authorization': 'Basic {}'.format(
				'{}:{}'.format(email, password).encode('base64').strip() )},
			decode='json', raise_for={401: AuthenticationError} )
		# Authorize token for Ubuntu One service
		self.auth_consumer = res['consumer_key'], res['consumer_secret']
		self.auth_token = res['token'], res['token_secret']
		yield self.request( self.auth_url_token,
			headers=self._auth_headers( self.auth_url_token,
				sign_method=oauth.SignatureMethod_PLAINTEXT() ) )
		defer.returnValue((self.auth_consumer, self.auth_token))


	def info_storage(self): return self()

	def info_public_files(self): return self('public_files')


	@defer.inlineCallbacks
	def volume_info(self, vol=None, type_filter=None):
		'Get list of all volumes or info for the one specified.'
		vols = yield self(join('volumes', vol), raise_for={404: DoesNotExist})
		if not isinstance(vols, list): vols = [vols]
		if type_filter is not None:
			vols = list(vol for vol in vols if vol['type'] == type_filter)
		if vol is not None: defer.returnValue(vols[0] if vols else None)
		defer.returnValue(vols)

	def volume_create(self, vol):
		'Create a new UDF volume to put files/dirs into (or to use with SyncTool).'
		return self(join('volumes', vol), method='put')

	def volume_delete(self, vol):
		'Delete named voulme.'
		return self(join('volumes', vol), method='delete')


	default_volume = None

	@defer.inlineCallbacks
	def get_default_volume(self):
		if not self.default_volume:
			vols = yield self.volume_info(type_filter='udf')
			if len(vols) == 1: self.default_volume = vols[0]['path']
			else:
				raise ValueError(( 'Unable to guess volume to use ({} available: {}),'
					' should be specified either as a call parameter or default_volume'
					' class attribute.' ).format(len(vols), ', '.join(op.itemgetter('path'), vols)))
		defer.returnValue(self.default_volume)

	def _prepend_volume(func):
		@defer.inlineCallbacks
		def _func(self, path, vol=None, **kwz):
			if not vol: vol = yield self.get_default_volume()
			defer.returnValue((yield func(self, join(vol, path), **kwz)))
		return _func

	def _content_path(func):
		@defer.inlineCallbacks
		def _func(self, path, vol=None, content_path=None, **kwz):
			if not content_path:
				if not vol: vol = yield self.get_default_volume()
				content_path = (yield self(join(vol, path), raise_for={404: DoesNotExist}))['content_path']
			defer.returnValue((yield func(self, content_path, **kwz)))
		return _func


	@_prepend_volume
	def node_info(self, path='', children=False):
		if children: children = 'true'
		return self(path, dict(include_children=children), raise_for={404: DoesNotExist})

	@_prepend_volume
	def node_delete(self, path=''):
		return self(path, method='delete')

	@_prepend_volume
	def node_mkdir(self, path=''):
		return self(path, data=dict(kind='directory'), encode='json', method='put')

	@_prepend_volume
	def node_touch(self, path=''):
		return self(path, data=dict(kind='file'), encode='json', method='put')

	@_prepend_volume
	def node_rename(self, path, new_path):
		return self(path, data=dict(path=new_path), encode='json', method='put')

	@_prepend_volume
	def node_set_public(self, path, value):
		return self(path, data=dict(is_public=value), encode='json', method='put')


	@_content_path
	def file_get(self, content_path):
		return self(content_path, content=True, decode=None)

	@_content_path
	def file_put_into(self, content_path, name, data):
		content_path = join(content_path, name)
		return self(content_path, content=True, method='put', data=data)

	@defer.inlineCallbacks
	def file_put(self, path, vol=None, content_path=None, data=None):
		assert data is not None
		if not content_path:
			if not vol: vol = yield self.get_default_volume()
			path, name = path.rsplit('/', 1)
			content_path = (yield self(join(vol, path), raise_for={404: DoesNotExist}))['content_path']
			content_path = join(content_path, name)
		defer.returnValue((yield self(content_path, content=True, method='put', data=data)))

	@_prepend_volume
	def file_put_magic(self, path, data):
		if isinstance(data, types.StringTypes): data = io.BytesIO(data)
		sha1, sha1_magic = hashlib.sha1(), hashlib.sha1('Ubuntu One')
		for chunk in iter(ft.partial(data.read, 2**20), ''):
			sha1.update(chunk), sha1_magic.update(chunk)
		return self(path, data=dict( kind='file',
				hash='sha1:{}'.format(sha1.hexdigest()),
				magic_hash='magic_hash:{}'.format(sha1_magic.hexdigest()) ),
			encode='json', method='put', raise_for={400: DoesNotExist})



if __name__ == '__main__':
	logging.basicConfig(level=logging.DEBUG)
	twisted_log.PythonLoggingObserver().start()

	req_pool_optz = txU1API.request_pool_options.copy()
	api = txU1API(debug_requests=True, request_pool_options=req_pool_optz)

	@defer.inlineCallbacks
	def test():
		try:
			api.auth_consumer, api.auth_token =\
				(open(n).read().splitlines() for n in ['u1_consumer', 'u1_token'])
		except (OSError, IOError):
			log.info('Getting new OAuth credentials')
			email = raw_input('U1 Email: ').strip()
			password = raw_input('U1 Password: ').strip()
			assert email and password, (email, password)
			auth = yield api.auth_create_token(email, password)
			open('u1_consumer', 'w').write('{}\n{}\n'.format(*api.auth_consumer))
			open('u1_token', 'w').write('{}\n{}\n'.format(*api.auth_token))
			log.info('Auth data acquired: {}'.format(auth))

		log.info('Storage info: {}'.format((yield api.info_storage())))
		log.info('Public files: {}'.format((yield api.info_public_files())))

		log.info('Volumes: {}'.format((yield api.volume_info())))

		try: vol_info = yield api.volume_info('~/test')
		except DoesNotExist: vol_info = yield api.volume_create('~/test')
		log.info('Volume: {}'.format(vol_info))
		api.default_volume = '~/test'

		try: log.info('dir info: {}'.format((yield api.node_info('/a/b/c', children=True))))
		except DoesNotExist: log.info('mkdir: {}'.format((yield api.node_mkdir('/a/b/c'))))

		contents = 'YAY'
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

	def done(res):
		if reactor.running: reactor.stop()
		if isinstance(res, (Exception, Failure)):
			log_web_failure(res)
		return res

	reactor.callWhenRunning(
		lambda: defer.maybeDeferred(test).addBoth(done) )
	reactor.run()

from contextlib import contextmanager
import datetime
import functools
import hashlib
import hmac
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
import uuid

import httpx

from sqlite_s3_query import sqlite_s3_query


class TestSqliteS3Query(unittest.TestCase):

    def test_select(self):
        db = get_db([
            "CREATE TABLE my_table (my_col_a text, my_col_b text);",
        ] + [
            "INSERT INTO my_table VALUES " + ','.join(["('some-text-a', 'some-text-b')"] * 500),
        ])

        put_object('my-bucket', 'my.db', db)

        with sqlite_s3_query('http://localhost:9000/my-bucket/my.db', get_credentials=lambda: (
            'us-east-1',
            'AKIAIOSFODNN7EXAMPLE',
            'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
            None,
        )) as query:
            with query('SELECT my_col_a FROM my_table') as (columns, rows):
                rows = list(rows)

        self.assertEqual(rows, [('some-text-a',)] * 500)

    def test_placeholder(self):
        db = get_db([
            "CREATE TABLE my_table (my_col_a text, my_col_b text);",
        ] + [
            "INSERT INTO my_table VALUES ('a','b'),('c','d')",
        ])

        put_object('my-bucket', 'my.db', db)

        with sqlite_s3_query('http://localhost:9000/my-bucket/my.db', get_credentials=lambda: (
            'us-east-1',
            'AKIAIOSFODNN7EXAMPLE',
            'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
            None,
        )) as query:
            with query("SELECT my_col_a FROM my_table WHERE my_col_b = ?", params=(('d',))) as (columns, rows):
                rows = list(rows)

        self.assertEqual(rows, [('c',)])

    def test_partial(self):
        db = get_db([
            "CREATE TABLE my_table (my_col_a text, my_col_b text);",
        ] + [
            "INSERT INTO my_table VALUES ('a','b'),('c','d')",
        ])

        put_object('my-bucket', 'my.db', db)

        query_my_db = functools.partial(sqlite_s3_query,
            url='http://localhost:9000/my-bucket/my.db',
            get_credentials=lambda: (
                'us-east-1',
                'AKIAIOSFODNN7EXAMPLE',
                'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
                None,
            )
        )

        with query_my_db() as query:
            with query("SELECT my_col_a FROM my_table WHERE my_col_b = ?", params=(('d',))) as (columns, rows):
                rows = list(rows)

        self.assertEqual(rows, [('c',)])

    def test_time_and_non_python_identifier(self):
        db = get_db(["CREATE TABLE my_table (my_col_a text, my_col_b text);"])

        put_object('my-bucket', 'my.db', db)

        with sqlite_s3_query('http://localhost:9000/my-bucket/my.db', get_credentials=lambda: (
            'us-east-1',
            'AKIAIOSFODNN7EXAMPLE',
            'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
            None,
        )) as query:
            now = datetime.datetime.utcnow()
            with query("SELECT date('now'), time('now')") as (columns, rows):
                rows = list(rows)

        self.assertEqual(rows, [(now.strftime('%Y-%m-%d'), now.strftime('%H:%M:%S'))])
        self.assertEqual(columns, ("date('now')", "time('now')"))

    def test_non_existant_table(self):
        db = get_db(["CREATE TABLE my_table (my_col_a text, my_col_b text);"])

        put_object('my-bucket', 'my.db', db)

        with sqlite_s3_query('http://localhost:9000/my-bucket/my.db', get_credentials=lambda: (
            'us-east-1',
            'AKIAIOSFODNN7EXAMPLE',
            'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
            None,
        )) as query:
            with self.assertRaises(Exception):
                query("SELECT * FROM non_table").__enter__()

    def test_empty_object(self):
        db = get_db(["CREATE TABLE my_table (my_col_a text, my_col_b text);"])

        put_object('my-bucket', 'my.db', b'')

        with self.assertRaises(Exception):
            sqlite_s3_query('http://localhost:9000/my-bucket/my.db', get_credentials=lambda: (
                'us-east-1',
                'AKIAIOSFODNN7EXAMPLE',
                'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
                None,
            )).__enter__()

    def test_bad_db_header(self):
        db = get_db(["CREATE TABLE my_table (my_col_a text, my_col_b text);"])

        put_object('my-bucket', 'my.db', b'*' * 100)

        with sqlite_s3_query('http://localhost:9000/my-bucket/my.db', get_credentials=lambda: (
            'us-east-1',
            'AKIAIOSFODNN7EXAMPLE',
            'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
            None,
        )) as query:
            with self.assertRaises(Exception):
                query("SELECT * FROM non_table").__enter__()

    def test_bad_db_second_half(self):
        db = get_db(["CREATE TABLE my_table (my_col_a text, my_col_b text);"] + [
            "INSERT INTO my_table VALUES " + ','.join(["('some-text-a', 'some-text-b')"] * 5000),
        ])

        half_len = int(len(db) / 2)
        db = db[:half_len] + len(db[half_len:]) * b'-'
        put_object('my-bucket', 'my.db', db)

        with sqlite_s3_query('http://localhost:9000/my-bucket/my.db', get_credentials=lambda: (
            'us-east-1',
            'AKIAIOSFODNN7EXAMPLE',
            'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
            None,
        )) as query:
            with self.assertRaises(Exception):
                with query("SELECT * FROM my_table") as (columns, rows):
                    list(rows)

    def test_num_connections(self):
        num_connections = 0

        @contextmanager
        def server():
            nonlocal num_connections
            def _run(server_sock):
                nonlocal num_connections

                while True:
                    try:
                        downstream_sock, _ = server_sock.accept()
                    except Exception:
                        break
                    num_connections += 1
                    connection_t = threading.Thread(target=handle_downstream, args=(downstream_sock,))
                    connection_t.start()

            with shutdown(get_new_socket()) as server_sock:
                server_sock.bind(('127.0.0.1', 9001))
                server_sock.listen(socket.IPPROTO_TCP)
                threading.Thread(target=_run, args=(server_sock,)).start()
                yield server_sock

        def get_http_client():
            @contextmanager
            def client():
                with httpx.Client() as original_client:
                    class Client():
                        def stream(self, method, url, headers):
                            parsed_url = urllib.parse.urlparse(url)
                            url = urllib.parse.urlunparse(parsed_url._replace(netloc='localhost:9001'))
                            return original_client.stream(method, url, headers=headers + (('host', 'localhost:9000'),))
                    yield Client()
            return client()

        with server() as server_sock:
            db = get_db([
                "CREATE TABLE my_table (my_col_a text, my_col_b text);",
            ] + [
                "INSERT INTO my_table VALUES " + ','.join(["('some-text-a', 'some-text-b')"] * 500),
            ])

            put_object('my-bucket', 'my.db', db)

            with sqlite_s3_query('http://localhost:9000/my-bucket/my.db', get_credentials=lambda: (
                'us-east-1',
                'AKIAIOSFODNN7EXAMPLE',
                'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
                None,
            ), get_http_client=get_http_client) as query:
                with query('SELECT my_col_a FROM my_table') as (columns, rows):
                    rows = list(rows)

            self.assertEqual(rows, [('some-text-a',)] * 500)
            self.assertEqual(num_connections, 1)

    def test_too_many_bytes(self):
        @contextmanager
        def server():
            def _run(server_sock):
                while True:
                    try:
                        downstream_sock, _ = server_sock.accept()
                    except Exception:
                        break
                    connection_t = threading.Thread(target=handle_downstream, args=(downstream_sock,))
                    connection_t.start()

            with shutdown(get_new_socket()) as server_sock:
                server_sock.bind(('127.0.0.1', 9001))
                server_sock.listen(socket.IPPROTO_TCP)
                threading.Thread(target=_run, args=(server_sock,)).start()
                yield server_sock

        def get_http_client():
            @contextmanager
            def client():
                with httpx.Client() as original_client:
                    class Client():
                        @contextmanager
                        def stream(self, method, url, headers):
                            parsed_url = urllib.parse.urlparse(url)
                            url = urllib.parse.urlunparse(parsed_url._replace(netloc='localhost:9001'))
                            range_query = dict(headers).get('range')
                            is_query = range_query and range_query != 'bytes=0-99'
                            with original_client.stream(method, url,
                                headers=headers + (('host', 'localhost:9000'),)
                            ) as response:
                                chunks = response.iter_bytes()
                                def iter_bytes(chunk_size=None):
                                    yield from chunks
                                    if is_query:
                                        yield b'e'
                                response.iter_bytes = iter_bytes
                                yield response
                    yield Client()
            return client()

        with server() as server_sock:
            db = get_db([
                "CREATE TABLE my_table (my_col_a text, my_col_b text);",
            ] + [
                "INSERT INTO my_table VALUES " + ','.join(["('some-text-a', 'some-text-b')"] * 500),
            ])

            put_object('my-bucket', 'my.db', db)

            with sqlite_s3_query('http://localhost:9000/my-bucket/my.db', get_credentials=lambda: (
                'us-east-1',
                'AKIAIOSFODNN7EXAMPLE',
                'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
                None,
            ), get_http_client=get_http_client) as query:
                with self.assertRaisesRegex(Exception, 'disk I/O error'):
                    query('SELECT my_col_a FROM my_table').__enter__()

    def test_disconnection(self):
        @contextmanager
        def server():
            def _run(server_sock):
                while True:
                    try:
                        downstream_sock, _ = server_sock.accept()
                    except Exception:
                        break
                    downstream_sock.close()
                    connection_t = threading.Thread(target=handle_downstream, args=(downstream_sock,))
                    connection_t.start()

            with shutdown(get_new_socket()) as server_sock:
                server_sock.bind(('127.0.0.1', 9001))
                server_sock.listen(socket.IPPROTO_TCP)
                threading.Thread(target=_run, args=(server_sock,)).start()
                yield server_sock

        def get_http_client():
            @contextmanager
            def client():
                with httpx.Client() as original_client:
                    class Client():
                        def stream(self, method, url, headers):
                            parsed_url = urllib.parse.urlparse(url)
                            url = urllib.parse.urlunparse(parsed_url._replace(netloc='localhost:9001'))
                            return original_client.stream(method, url, headers=headers + (('host', 'localhost:9000'),))
                    yield Client()
            return client()

        with server() as server_sock:
            db = get_db([
                "CREATE TABLE my_table (my_col_a text, my_col_b text);",
            ] + [
                "INSERT INTO my_table VALUES " + ','.join(["('some-text-a', 'some-text-b')"] * 500),
            ])

            put_object('my-bucket', 'my.db', db)

        with self.assertRaises(Exception):
            sqlite_s3_query('http://localhost:9000/my-bucket/my.db', get_credentials=lambda: (
                'us-east-1',
                'AKIAIOSFODNN7EXAMPLE',
                'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
                None,
            ), get_http_client=get_http_client).__enter__()

def put_object(bucket, key, content):
    create_bucket(bucket)
    enable_versioning(bucket)

    url = f'http://127.0.0.1:9000/{bucket}/{key}'
    body_hash = hashlib.sha256(content).hexdigest()
    parsed_url = urllib.parse.urlsplit(url)

    headers = aws_sigv4_headers(
        'AKIAIOSFODNN7EXAMPLE', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
        (), 's3', 'us-east-1', parsed_url.netloc, 'PUT', parsed_url.path, (), body_hash,
    )
    response = httpx.put(url, content=content, headers=headers)
    response.raise_for_status()

def create_bucket(bucket):
    url = f'http://127.0.0.1:9000/{bucket}/'
    content = b''
    body_hash = hashlib.sha256(content).hexdigest()
    parsed_url = urllib.parse.urlsplit(url)

    headers = aws_sigv4_headers(
        'AKIAIOSFODNN7EXAMPLE', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
        (), 's3', 'us-east-1', parsed_url.netloc, 'PUT', parsed_url.path, (), body_hash,
    )
    response = httpx.put(url, content=content, headers=headers)

def enable_versioning(bucket):
    content = '''
        <VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
            <Status>Enabled</Status>
        </VersioningConfiguration>
    '''.encode()
    url = f'http://127.0.0.1:9000/{bucket}/?versioning'
    body_hash = hashlib.sha256(content).hexdigest()
    parsed_url = urllib.parse.urlsplit(url)

    headers = aws_sigv4_headers(
        'AKIAIOSFODNN7EXAMPLE', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
        (), 's3', 'us-east-1', parsed_url.netloc, 'PUT', parsed_url.path, (('versioning', ''),), body_hash,
    )
    response = httpx.put(url, content=content, headers=headers)
    response.raise_for_status()


def aws_sigv4_headers(access_key_id, secret_access_key, pre_auth_headers,
                      service, region, host, method, path, params, body_hash):
    algorithm = 'AWS4-HMAC-SHA256'

    now = datetime.datetime.utcnow()
    amzdate = now.strftime('%Y%m%dT%H%M%SZ')
    datestamp = now.strftime('%Y%m%d')
    credential_scope = f'{datestamp}/{region}/{service}/aws4_request'

    pre_auth_headers_lower = tuple((
        (header_key.lower(), ' '.join(header_value.split()))
        for header_key, header_value in pre_auth_headers
    ))
    required_headers = (
        ('host', host),
        ('x-amz-content-sha256', body_hash),
        ('x-amz-date', amzdate),
    )
    headers = sorted(pre_auth_headers_lower + required_headers)
    signed_headers = ';'.join(key for key, _ in headers)

    def signature():
        def canonical_request():
            canonical_uri = urllib.parse.quote(path, safe='/~')
            quoted_params = sorted(
                (urllib.parse.quote(key, safe='~'), urllib.parse.quote(value, safe='~'))
                for key, value in params
            )
            canonical_querystring = '&'.join(f'{key}={value}' for key, value in quoted_params)
            canonical_headers = ''.join(f'{key}:{value}\n' for key, value in headers)

            return f'{method}\n{canonical_uri}\n{canonical_querystring}\n' + \
                   f'{canonical_headers}\n{signed_headers}\n{body_hash}'

        def sign(key, msg):
            return hmac.new(key, msg.encode('ascii'), hashlib.sha256).digest()

        string_to_sign = f'{algorithm}\n{amzdate}\n{credential_scope}\n' + \
                         hashlib.sha256(canonical_request().encode('ascii')).hexdigest()

        date_key = sign(('AWS4' + secret_access_key).encode('ascii'), datestamp)
        region_key = sign(date_key, region)
        service_key = sign(region_key, service)
        request_key = sign(service_key, 'aws4_request')
        return sign(request_key, string_to_sign).hex()

    return (
        (b'authorization', (
            f'{algorithm} Credential={access_key_id}/{credential_scope}, '
            f'SignedHeaders={signed_headers}, Signature=' + signature()).encode('ascii')
         ),
        (b'x-amz-date', amzdate.encode('ascii')),
        (b'x-amz-content-sha256', body_hash.encode('ascii')),
    ) + pre_auth_headers


def get_db(sqls):
    with tempfile.NamedTemporaryFile() as fp:
        with sqlite3.connect(fp.name, isolation_level=None) as con:
            cur = con.cursor()
            for sql in sqls:
                cur.execute(sql)

        with open(fp.name, 'rb') as f:
            return f.read()

def get_new_socket():
    sock = socket.socket(family=socket.AF_INET, type=socket.SOCK_STREAM,
                         proto=socket.IPPROTO_TCP)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock

def upstream_connect():
    upstream_sock = socket.create_connection(('127.0.0.1', 9000))
    upstream_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return upstream_sock

@contextmanager
def shutdown(sock):
    try:
        yield sock
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            sock.close()

def proxy(done, source, target):
    try:
        chunk = source.recv(1)
        while chunk:
            target.sendall(chunk)
            chunk = source.recv(1)
    except OSError:
        pass
    finally:
        done.set()

def handle_downstream(downstream_sock):
    with \
            shutdown(upstream_connect()) as upstream_sock, \
            shutdown(downstream_sock) as downstream_sock:

        done = threading.Event()
        threading.Thread(target=proxy, args=(done, upstream_sock, downstream_sock)).start()
        threading.Thread(target=proxy, args=(done, downstream_sock, upstream_sock)).start()
        done.wait()

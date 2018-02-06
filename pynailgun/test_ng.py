import subprocess
import os
import time
import StringIO
import unittest
import tempfile
import shutil
import uuid
import sys

from pynailgun import NailgunException, NailgunConnection


if os.name == 'posix':
    def transport_exists(transport_file):
        return os.path.exists(transport_file)


if os.name == 'nt':
    import ctypes
    from ctypes.wintypes import WIN32_FIND_DATAW as WIN32_FIND_DATA
    INVALID_HANDLE_VALUE = -1
    FindFirstFile = ctypes.windll.kernel32.FindFirstFileW
    FindClose = ctypes.windll.kernel32.FindClose

    # on windows os.path.exists doen't allow to check reliably that a pipe exists
    # (os.path.exists tries to open connection to a pipe)
    def transport_exists(transport_path):
        wfd = WIN32_FIND_DATA()
        handle = FindFirstFile(transport_path, ctypes.byref(wfd))
        result = handle != INVALID_HANDLE_VALUE
        FindClose(handle)
        return result


class TestNailgunConnection(unittest.TestCase):
    def setUp(self):
        self.setUpTransport()
        self.startNailgun()

    def setUpTransport(self):
        self.tmpdir = tempfile.mkdtemp()
        if os.name == 'posix':
            self.transport_file = os.path.join(self.tmpdir, 'sock')
            self.transport_address = 'local:{0}'.format(self.transport_file)
        else:
            pipe_name = u'nailgun-test-{0}'.format(uuid.uuid4().hex)
            self.transport_address = u'local:{0}'.format(pipe_name)
            self.transport_file = ur'\\.\pipe\{0}'.format(pipe_name)

    def getClassPath(self):
        cp = [
            'nailgun-server/target/nailgun-server-0.9.3-SNAPSHOT-uber.jar',
            'nailgun-examples/target/nailgun-examples-0.9.3-SNAPSHOT.jar',
            ]
        if os.name == 'nt':
            return ';'.join(cp)
        return ':'.join(cp)

    def startNailgun(self):
        if os.name == 'posix':
            def preexec_fn():
                # Close any open file descriptors to further separate buckd from its
                # invoking context (e.g. otherwise we'd hang when running things like
                # `ssh localhost buck clean`).
                dev_null_fd = os.open("/dev/null", os.O_RDWR)
                os.dup2(dev_null_fd, 0)
                os.dup2(dev_null_fd, 2)
                os.close(dev_null_fd)
            creationflags = 0
        else:
            preexec_fn = None
            # https://msdn.microsoft.com/en-us/library/windows/desktop/ms684863.aspx#DETACHED_PROCESS
            DETACHED_PROCESS = 0x00000008
            creationflags = DETACHED_PROCESS

        stdout = None
        if os.name == 'posix':
            stdout=subprocess.PIPE

        cmd = ['java', '-Djna.nosys=true', '-classpath', self.getClassPath()]
        debug_mode = os.environ.get('DEBUG_MODE') or ''
        if debug_mode != '':
            suspend = 'n' if debug_mode == '2' else 'y'
            cmd.append('-agentlib:jdwp=transport=dt_socket,address=localhost:8888,server=y,suspend=' + suspend)
        cmd = cmd + ['com.martiansoftware.nailgun.NGServer', self.transport_address]

        self.ng_server_process = subprocess.Popen(
            cmd,
            preexec_fn=preexec_fn,
            creationflags=creationflags,
            stdout=stdout,
        )

        self.assertIsNone(self.ng_server_process.poll())

        if os.name == 'posix':
            # on *nix we have to wait for server to be ready to accept connections
            while True:
                the_first_line = self.ng_server_process.stdout.readline().strip()
                if "NGServer" in the_first_line and "started" in the_first_line:
                    break
                if the_first_line is None or the_first_line == '':
                    break
        else:
            for _ in range(0, 600):
                # on windows it is OK to rely on existence of the pipe file
                if not transport_exists(self.transport_file):
                    time.sleep(0.01)
                else:
                    break

        self.assertTrue(transport_exists(self.transport_file))

    def test_nailgun_stats(self):
        output = StringIO.StringIO()
        with NailgunConnection(
                self.transport_address,
                stderr=None,
                stdin=None,
                stdout=output) as c:
            exit_code = c.send_command('ng-stats')
        self.assertEqual(exit_code, 0)
        actual_out = output.getvalue().strip()
        expected_out = 'com.martiansoftware.nailgun.builtins.NGServerStats: 1/1'
        self.assertEqual(actual_out, expected_out)

    def test_nailgun_exit_code(self):
        output = StringIO.StringIO()
        expected_exit_code = 10
        with NailgunConnection(
                self.transport_address,
                stderr=None,
                stdin=None,
                stdout=output) as c:
            exit_code = c.send_command('com.martiansoftware.nailgun.examples.Exit', [str(expected_exit_code)])
        self.assertEqual(exit_code, expected_exit_code)

    def test_nailgun_stdin(self):
        lines = [str(i) for i in range(100)]
        echo = '\n'.join(lines)
        output = StringIO.StringIO()
        input = StringIO.StringIO(echo)
        with NailgunConnection(
                self.transport_address,
                stderr=None,
                stdin=input,
                stdout=output) as c:
            exit_code = c.send_command('com.martiansoftware.nailgun.examples.Echo')
        self.assertEqual(exit_code, 0)
        actual_out = output.getvalue().strip()
        self.assertEqual(actual_out, echo)

    def test_nailgun_default_streams(self):
        with NailgunConnection(self.transport_address) as c:
            exit_code = c.send_command('ng-stats')
        self.assertEqual(exit_code, 0)

    def tearDown(self):
        try:
            with NailgunConnection(
                self.transport_address,
                cwd=os.getcwd(),
                stderr=None,
                stdin=None,
                stdout=None) as c:
                c.send_command('ng-stop')
        except NailgunException as e:
            # stopping server is a best effort
            # if something wrong has happened, we will kill it anyways
            pass

        # Python2 compatible wait with timeout
        process_exit_code = None
        for _ in range(0, 500):
            process_exit_code = self.ng_server_process.poll()
            if process_exit_code is not None:
                break
            time.sleep(0.02)   # 1 second total

        if process_exit_code is None:
            # some test has failed, ng-server was not stopped. killing it
            self.ng_server_process.kill()
        shutil.rmtree(self.tmpdir)


if __name__ == '__main__':
    for i in range(10):
        was_successful = unittest.main(exit=False).result.wasSuccessful()
        if not was_successful:
            sys.exit(1)

import atexit
import cmd
import getpass
import grp
import inspect
import os
import pwd
import re
import readline
import shlex
import stat
import sys
import textwrap
import urlparse
import zlib

from errors import WebHDFSError
from client import WebHDFSClient
from attrib import LocalFSObject

# Work around python's overeager completer delimiters
readline.set_completer_delims(readline.get_completer_delims().translate(None, '-'))

class WebHDFSPrompt(cmd.Cmd):
    def __init__(self, base, conf=None, path=None, task=None, wait=None):
        cmd.Cmd.__init__(self)

        self.base = urlparse.urlparse(base)
        self.user = getpass.getuser()
        self.hdfs = WebHDFSClient(self.base._replace(path='').geturl(), self.user, conf, wait)

        self.do_cd(path or self.base.path)

        if task:
            self.onecmd(' '.join(task))
            sys.exit(0)

        try:
            self.hist = os.path.join(os.path.expanduser('~'), os.environ.get('WEBHDFS_HISTFILE', '.webhdfs_history'))
            readline.read_history_file(self.hist)

        except IOError:
            pass

        try:
            readline.set_history_length(int(os.environ.get('WEBHDFS_HISTSIZE', 3)))
        except ValueError:
            readline.set_history_length(0)

        if os.access(self.hist, os.W_OK):
            atexit.register(readline.write_history_file, self.hist)


    def _list_dir(self, sources):
        subdirs = []
        objects = []
        columns = ['mode', 'repl', 'owner', 'group', 'size', 'date', 'name']
        lengths = dict(zip(columns, [0] * len(columns)))
        build = {
            'date': '{:%b %d %Y %H:%M:%S}',
        }
        align = {
            'repl': '>',
            'size': '>',
        }

        for item in sources:
            if not stat.S_ISREG(item.perm) and not stat.S_ISDIR(item.perm):
                continue

            if stat.S_ISDIR(item.perm):
                subdirs.append(item.name)

            tmp_obj = {}

            for name in columns:
                text = build.get(name, '{}').format(getattr(item, name))

                tmp_obj[name] = text
                lengths[name] = max(lengths[name], len(text))

            objects.append(tmp_obj)

        text = ' '.join('{%s:%s%s}' % (i, align.get(i, ''), lengths[i]) for i in columns)
        for item in objects:
            print text.format(**item)

        return subdirs

    def _fix_path(self, path, local=False, required=False):
        path = '' if path is None else path.strip()
        rval = []

        if not path and required:
            raise WebHDFSError('%s: path not specified' % required)

        if not path:
            path = getattr(self, 'path', '/user/%s' % self.user) if not local else os.getcwd()

        if not path.startswith('/'):
            path = '%s/%s' % (self.path if not local else os.getcwd(), path)

        for part in path.split('/'):
            if not part or part == '.':
                continue
            if rval and part == '..':
                rval.pop()
            else:
                rval.append(part)

        return '/'+'/'.join(rval)

    def _print_usage(self):
        print getattr(self, inspect.stack()[1][3]).__doc__.strip().split('\n')[0]

    def _reset_prompt(self):
        self.prompt = '%s@%s r:%s l:%s> ' % (self.user, self.base.netloc, self.path, os.getcwd())

    def _complete_local(self, part, kind):
        path = self._fix_path(part, local=True)
        name = ''

        if part and not part.endswith('/'):
            name = os.path.basename(path)
            path = os.path.dirname(path)

        if kind == 'file':
            pick = lambda x: x.startswith(name) and not stat.S_ISDIR(os.stat('%s/%s' % (path, x)).st_mode)
        elif kind == 'dir':
            pick = lambda x: x.startswith(name) and stat.S_ISDIR(os.stat('%s/%s' % (path, x)).st_mode)
        else:
            pick = lambda x: x.startswith(name)

        return [i + ('/' if stat.S_ISDIR(os.stat('%s/%s' % (path, i)).st_mode) else ' ') for i in os.listdir(path) if pick(i)]

    def _complete_remote(self, part, kind):
        path = self._fix_path(part)
        name = ''

        if part and not part.endswith('/') and not part.endswith('/..'):
            name = os.path.basename(path)
            path = os.path.dirname(path)

        if kind == 'file':
            pick = lambda x: x.name.startswith(name) and not x.is_dir()
        elif kind == 'dir':
            pick = lambda x: x.name.startswith(name) and x.is_dir()
        else:
            pick = lambda x: x.name.startswith(name)

        return [i.name + ('/' if i.is_dir() and not i.is_empty() else ' ') for i in self.hdfs.ls(path, request=pick)]

    def _complete_du(self, part, cache=[]):
        if not cache:
            cache.extend(['dirs', 'files', 'hdfs_usage', 'disk_usage', 'hdfs_quota', 'disk_usage'])

        rval = [i for i in cache if i.startswith(part)]
        return rval if len(rval) != 1 else [rval[0] + ' ']

    def _complete_chown(self, part, cache={}):
        if ':' not in part:
            if 'pwd' not in cache:
                cache['pwd'] = pwd.getpwall()

            rval = [i.pw_name for i in cache['pwd'] if i.pw_name.startswith(part)]
            return rval if len(rval) != 1 else [rval[0] + ':']
        else:
            if 'grp' not in cache:
                cache['grp'] = grp.getgrall()

            rval = [i.gr_name for i in cache['grp'] if i.gr_name.startswith(part.split(':', 1)[-1])]
            return rval if len(rval) != 1 else [rval[0] + ' ']

    def _complete_chmod(self, part):
        mode = int(part, 8) if part else 0
        if len(part) < 4 and (mode << 3) < 0777:
            return [oct((mode << 3) + i) for i in range(1, 8)]
        else:
            return [oct(mode) + ' ']

    def completedefault(self, part, line, s, e):
        if part == '.' or part == '..':
            return [part + '/']

        args = shlex.split(line[:e])
        if len(args) == 1 or line[e - 1] == ' ':
            args.append('')

        # Extract completion magic from method documentation
        docs = getattr(getattr(self, 'do_'+args[0], object), '__doc__')
        rule = re.findall(r'(?:[<\[](.+?)[>\]])+', docs)[len(args) - 2]

        if re.search(r'(?:local|remote) (?:file/dir|file|dir)', rule):
            kind, dest = rule.split()
            return getattr(self, '_complete_'+kind)(args[-1], dest)
        if re.search(r'\w+ options', rule):
            return getattr(self, '_complete_'+rule.split()[0])(args[-1])

    def emptyline(self):
        pass

    def default(self, arg):
        print '%s: unknown command' % arg

    def do_cd(self, path=None):
        '''
            Usage: cd <remote dir>

            Changes the shell remote directory
        '''
        try:
            path = self._fix_path(path or '/user/%s' % self.user)
            if not self.hdfs.stat(path).is_dir():
                raise WebHDFSError('%s: not a directory' % path)
            self.path = path
        except WebHDFSError as e:
            self.path = '/'
            print e
        finally:
            self._reset_prompt()

    def do_lcd(self, path=None):
        '''
            Usage: lcd <local dir>

            Changes the shell local directory
        '''
        try:
            path = self._fix_path(path or pwd.getpwnam(self.user).pw_dir, local=True)
            os.chdir(path)
        except (KeyError, OSError) as e:
            print e
        finally:
            self._reset_prompt()

    def do_ls(self, path=None):
        '''
            Usage: ls <remote file/dir>

            Lists remote file or directory
        '''
        try:
            path = self._fix_path(path)
            self._list_dir(self.hdfs.ls(path))
        except WebHDFSError as e:
            print e

    def do_lsr(self, path=None):
        '''
            Usage: ls <remote file/dir>

            Lists remote file or directory recursively
        '''
        try:
            path = self._fix_path(path)
            print path + ':'
            for name in self._list_dir(self.hdfs.ls(path)):
                print
                self.do_lsr('%s/%s' % (path, name))
        except WebHDFSError as e:
            print e

    def do_glob(self, path=None):
        '''
            Usage: glob <remote file/dir>

            Lists remote file or directory pattern
        '''
        try:
            path = self._fix_path(path, required='glob')
            self._list_dir(self.hdfs.glob(path))
        except WebHDFSError as e:
            print e

    def do_lls(self, path=None):
        '''
            Usage: lls <local file/dir>

            Lists local file or directory
        '''
        try:
            path = self._fix_path(path, local=True)
            info = os.stat(path)
            objs = []

            if stat.S_ISDIR(info.st_mode):
                objs = list(LocalFSObject(path, name) for name in os.listdir(path))
            elif stat.S_ISREG(info.st_mode):
                objs = [LocalFSObject(os.path.dirname(path), os.path.basename(path))]

            self._list_dir(objs)
        except OSError as e:
            print e

    def do_du(self, args=''):
        '''
            Usage: du <remote file/dir> [du options]

            Options: dirs|files|hdfs_usage|disk_usage|hdfs_quota|disk_quota

            Displays usage for remote file or directory
        '''
        try:
            args = shlex.split(args)
            if len(args) > 2:
                return self._print_usage()

            path = self._fix_path(args[0] if len(args) > 0 else None)
            print self.hdfs.du(path, args[1] if len(args) == 2 else 'hdfs_usage')
        except WebHDFSError as e:
            print e

    def do_mkdir(self, path):
        '''
            Usage: mkdir <remote dir>

            Creates remote directory
        '''
        try:
            path = self._fix_path(path, required='mkdir')
            if self.hdfs.stat(path, catch=True):
                raise WebHDFSError('%s: already exists' % path)
            self.hdfs.mkdir(path)
        except WebHDFSError as e:
            print e

    def do_mv(self, args):
        '''
            Usage: mv <remote file/dir> <remote dir>

            Moves/renames remote file or directory
        '''
        try:
            path, dest = shlex.split(args)
            path = self._fix_path(path, required='mv')
            dest = self._fix_path(dest, required='mv')
            stat = self.hdfs.stat(dest, catch=True) or self.hdfs.stat(os.path.dirname(dest), catch=True)
            if stat and not stat.is_dir():
                raise WebHDFSError('%s: invalid destination' % dest)
            if not self.hdfs.mv(path, dest):
                raise WebHDFSError('%s: failed to move/rename' % path)
        except WebHDFSError as e:
            print e
        except ValueError as e:
            self._print_usage()

    def do_rm(self, path):
        '''
            Usage: rm <remote file>

            Removes remote file
        '''
        try:
            path = self._fix_path(path, required='rm')
            if self.hdfs.stat(path).is_dir():
                raise WebHDFSError('%s: cannot remove directory' % path)
            self.hdfs.rm(path)
        except WebHDFSError as e:
            print e

    def do_rmdir(self, path):
        '''
            Usage: rm <remote dir>

            Removes remote directory
        '''
        try:
            path = self._fix_path(path, required='rmdir')
            temp = self.hdfs.stat(path)
            if not temp.is_dir():
                raise WebHDFSError('%s: not a directory' % path)
            if not temp.is_empty():
                raise WebHDFSError('%s: directory not empty' % path)
            self.hdfs.rm(path)
        except WebHDFSError as e:
            print e

    def do_chown(self, args):
        '''
            Usage: chown <chown options> <remote file/dir>

            Options: [owner][:group]

            Change ownership of remote file or directory
        '''
        try:
            dest, path = shlex.split(args)
            path = self._fix_path(path, required='chown')
            o, g = dest.split(':', 1) if ':' in dest else (dest, '')
            self.hdfs.chown(path, owner=o, group=g)
        except WebHDFSError as e:
            print e
        except ValueError:
            self._print_usage()

    def do_chmod(self, args):
        '''
            Usage: chmod <chmod options> <remote file/dir>

            Options: octal mode: 0000 - 0777

            Change permission on remote file or directory
        '''
        try:
            perm, path = shlex.split(args)
            path = self._fix_path(path, required='chmod')
            self.hdfs.chmod(path, perm)
        except WebHDFSError as e:
            print e
        except ValueError:
            self._print_usage()

    def do_touch(self, args):
        '''
            Usage touch <remote file> [epoch time]

            Change modification time on remote file, optionally creating it
        '''
        try:
            args = shlex.split(args)
            if len(args) > 2:
                return self._print_usage()

            path = self._fix_path(args[0])
            time = None
            try:
                time = int(args[1])
            except Exception as e:
                if not isinstance(e, (ValueError, IndexError)):
                    self._print_usage()

            self.hdfs.touch(path, time)
        except WebHDFSError as e:
            print e

    def do_get(self, path):
        '''
            Usage: get <remote file>

            Fetch remote file into current local directory
        '''
        try:
            path = self._fix_path(path, required='get')
            if self.hdfs.stat(path).is_dir():
                raise WebHDFSError('%s: cannot download directory' % path)
            if os.path.exists(os.path.basename(path)):
                raise WebHDFSError('%s: file exists' % path)
            self.hdfs.get(path, data=open('%s/%s' % (os.getcwd(), os.path.basename(path)), 'w'))
        except (WebHDFSError, OSError) as e:
            print e

    def do_put(self, path):
        '''
            Usage: put <local file>

            Upload local file into current remote directory
        '''
        try:
            path = self._fix_path(path, local=True, required='put')
            dest = '%s/%s' % (self.path, os.path.basename(path))
            if stat.S_ISDIR(os.stat(path).st_mode):
                raise WebHDFSError('%s: cannot upload directory' % path)
            if self.hdfs.stat(dest, catch=True):
                raise WebHDFSError('%s: already exists' % dest)
            self.hdfs.put(dest, data=open(path, 'r'))
        except (WebHDFSError, OSError) as e:
            print e

    def do_cat(self, path):
        '''
            Usage: cat <remote file>

            Display contents of remote file
        '''
        try:
            path = self._fix_path(path, required='cat')
            if self.hdfs.stat(path).is_dir():
                raise WebHDFSError('%s: cannot cat directory' % path)
            sys.stdout.write(self.hdfs.get(path))
        except (WebHDFSError, OSError) as e:
            print e

    def do_zcat(self, path):
        '''
            Usage: zcat <remote file>

            Display contents of compressed remote file
        '''
        try:
            path = self._fix_path(path, required='zcat')
            if self.hdfs.stat(path).is_dir():
                raise WebHDFSError('%s: cannot cat directory' % path)
            sys.stdout.write(zlib.decompress(self.hdfs.get(path), 16 + zlib.MAX_WBITS))
        except (WebHDFSError, OSError) as e:
            print e

    def do_EOF(self, line):
        print
        return True

for name, func in vars(WebHDFSPrompt).items():
    if name.startswith('do_') and getattr(func, '__doc__'):
        func.__doc__ = textwrap.dedent(func.__doc__).strip()

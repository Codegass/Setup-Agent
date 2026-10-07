"""Called by the frozen /etc/mavenrc; observe argv and preserve existing options.

No credentials, task answers, source contents or resolved settings are emitted.
The observer is intentionally disabled outside a Git checkout under /workspace.
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import stat
import urllib.request

BASE = Path('/opt/sag-maven-witness')


def main():
    def git(*args):
        return subprocess.check_output(['git', *args], stderr=subprocess.DEVNULL).decode().strip()
    try:
        args = sys.argv[1:]
        launcher_pid = None
        if args[:1] == ['--launcher-pid']:
            launcher_pid = int(args[1])
            args = args[2:]
        root = Path(git('rev-parse', '--show-toplevel')).resolve()
        if not root.is_relative_to(Path('/workspace')):
            return
        # Preserve an existing Maven core extension classpath; avoid last-D-wins.
        original = os.environ.get('MAVEN_OPTS', '')
        words = shlex.split(original)
        extensions = [w for w in words if w.startswith('-Dmaven.ext.class.path=')]
        if len(extensions) > 1:
            return  # Ambiguous launcher configuration is not rewritten.
        jar = str(BASE / 'witness.jar')
        if extensions:
            old = extensions[0].partition('=')[2]
            words[words.index(extensions[0])] = '-Dmaven.ext.class.path=' + old + ':' + jar
        else:
            words.append('-Dmaven.ext.class.path=' + jar)
        # Maven's shell launcher word-splits MAVEN_OPTS without eval. Quoted
        # whitespace is not safely round-trippable; don't pretend otherwise.
        if any(any(c.isspace() for c in w) for w in words):
            return
        launch = {'argv': args, 'cwd': os.getcwd(), 'root': str(root), 'launcher_pid': launcher_pid,
                  # Whitelisted effective launcher inputs, before our extension
                  # is appended. Native tool hooks use these to detect shell
                  # environment changes; never forward the general environment.
                  'runtime_inputs': {key: os.environ.get(key, '') for key in (
                      'MAVEN_ARGS', 'MAVEN_OPTS', 'MAVEN_CONFIG', 'MAVEN_PROJECTBASEDIR',
                      'MAVEN_SKIP_RC', 'JAVA_TOOL_OPTIONS', '_JAVA_OPTIONS', 'JDK_JAVA_OPTIONS')},
                  'commit': git('rev-parse', 'HEAD'), 'tracked_clean': not git('diff', '--name-only', 'HEAD', '--'),
                  'observer_sha256': hashlib.sha256((BASE/'witness.jar').read_bytes()).hexdigest(),
                  'activation_sha256': hashlib.sha256(Path('/etc/mavenrc').read_bytes()).hexdigest()}
        # A native launcher callback observes resolved argv/cwd/environment;
        # it never changes the actor's shell program. Read the launcher's FDs,
        # not this command-substitution child's stdout pipe.
        if launcher_pid and os.environ.get('BENCH_OBSERVER_URL'):
            try:
                stdout = Path('/proc')/str(launcher_pid)/'fd/1'
                stderr = Path('/proc')/str(launcher_pid)/'fd/2'
                a, b = stdout.stat(), stderr.stat()
                output_path = str(stdout.resolve()) if stat.S_ISREG(a.st_mode) and (a.st_dev,a.st_ino)==(b.st_dev,b.st_ino) else None
                event = {'hook_event_name': 'MavenLaunch', 'launch': launch, 'output_path': output_path,
                         'namespace': os.environ['HOSTNAME'], 'observer_environment': {
                             k: os.environ[k] for k in ('PATH','HOME','JAVA_HOME','MAVEN_HOME','M2_HOME',
                                 'MAVEN_ARGS','MAVEN_OPTS','MAVEN_CONFIG','MAVEN_PROJECTBASEDIR','MAVEN_SKIP_RC',
                                 'JAVA_TOOL_OPTIONS','_JAVA_OPTIONS','JDK_JAVA_OPTIONS') if k in os.environ}}
                request = urllib.request.Request(os.environ['BENCH_OBSERVER_URL']+'/event',
                    data=json.dumps(event).encode(),headers={'Content-Type':'application/json',
                        'Authorization':'Bearer '+os.environ['BENCH_OBSERVER_TOKEN']})
                with urllib.request.urlopen(request,timeout=180) as response: response.read()
            except Exception:
                pass  # A collector failure must never fail or alter the build.
        values = {'MAVEN_OPTS': ' '.join(words), 'BENCH_MAVEN_WITNESS_ROOT': str(root),
                  'BENCH_MAVEN_WITNESS_RUN_ID': os.environ['HOSTNAME'],
                  'BENCH_MAVEN_WITNESS_DIR': '/opt/sag-maven-observations',
                  'BENCH_MAVEN_WITNESS_LAUNCH': json.dumps(launch, separators=(',', ':'))}
        for key, value in values.items():
            print('export ' + key + '=' + shlex.quote(value))
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        return  # Missing passive evidence never turns a build into a failure.


if __name__ == '__main__':
    main()

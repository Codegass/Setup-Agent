"""One prospectively recorded harness attempt in a disposable Docker container.

The host owns the definitions, ledger, scorer and deadline. The agent receives
only a task brief in a fresh checkout. No post-agent worktree cleanup is allowed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request

from sag.benchmark.recorder import reference, write_json
from sag.benchmark.requirements import canonical_digest, evaluation_identity, load_json, normalize_task, validate_requirements
from sag.benchmark import intervention_protocol
from scripts.benchmark_gateway_usage import read_usage
from scripts.export_requirements_evaluator import export_runtime

ROOT = Path(__file__).resolve().parents[1]
MEMORY = 8 * 1024**3
RESERVE = 32 * 1024**3
POLICY = {'version': 'sag-unattended-v1', 'scope': 'task_affecting_human_actions',
          'channels': {'stdin': 'disabled', 'runner_control': 'disabled_or_logged', 'external_actions': 'prohibited_or_logged'},
          'window': 'process_start_to_termination', 'initial_task_excluded': True,
          'operator_commitment': 'report_any_task_affecting_deviation', 'manual_cancellation': 'counted'}


def now():
    return datetime.now(timezone.utc).isoformat()


def call(argv, *, timeout=60, env=None):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, env=env)
    if result.returncode:
        raise RuntimeError(str(argv[:4]) + ': ' + result.stderr.decode(errors='replace')[-2000:])
    return result.stdout.decode().strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sag_model_config(protocol, *, image, seconds, no_advisor=False):
    """Reject misplaced experiment settings instead of silently running defaults."""
    from sag.config.settings import Config

    misplaced = sorted(set(protocol) & set(Config.model_fields))
    if misplaced:
        raise ValueError(f'Model settings belong inside model_config: {misplaced}')
    config = dict(protocol['model_config'])
    config.pop('ui_mode', None)  # Historical display-only option, not a Config field.
    unknown = sorted(set(config) - set(Config.model_fields))
    if unknown:
        raise ValueError(f'Unknown model settings: {unknown}')
    if no_advisor:
        config['advisor_mode'] = 'off'
    else:
        config.setdefault('advisor_mode', 'openai/gpt-5.4-mini')
    config.update(docker_base_image=image, max_wall_clock_seconds=seconds)
    Config.model_validate(config)
    return config


def effective_sag_config(config, environment, directory):
    """Check the exact child environment before any model request is sent."""
    probe = subprocess.run(
        [sys.executable, '-c',
         'import json; from sag.config.settings import Config; '
         'from sag.agent.control_events import sanitize_config; '
         'print(json.dumps(sanitize_config(Config.from_env())))'],
        cwd=directory, env=environment, capture_output=True, text=True, timeout=60,
    )
    if probe.returncode:
        raise RuntimeError('Could not verify SAG effective configuration')
    actual = json.loads(probe.stdout)
    changed = sorted(key for key, value in config.items() if actual.get(key) != value)
    if changed:
        raise ValueError(f'Effective SAG configuration differs: {changed}')
    return actual


def prompt(task, checkout, seconds, requirements=None, *, runtime_handoff=False):
    rows = []
    for step in task['steps']:
        rows.append({'id': step['id'], 'cwd': step['cwd'], 'argv': step['argv'],
                     'java_major': step.get('java_major'), 'maven_version': step.get('maven_version')})
        declared = next((s for s in (requirements or {}).get('steps', []) if s.get('step_id') == step['id']), {})
        if declared.get('environment'):
            rows[-1]['environment'] = declared['environment']
    return (f'Set up the Java project in {checkout} at pinned commit {task["sha"]}. '
            'If the checkout is absent, clone https://github.com/'+task['repo']+'.git and check out that commit first. '
            'Install/configure the required tools and complete all of the following ordered build/test commands. '
            'Preserve the exact command arguments and source commit. Never modify or delete tracked repository files. '
            'New files and tool configuration outside the checkout are allowed. Keep diagnostic logs outside the checkout. '
            'There is no human feedback. Network is available for dependencies and toolchains; do not retrieve benchmark '
            'answers, earlier attempts or other agents\' outputs. Do not publish to any remote repository.\n\n'
            + json.dumps(rows, indent=2) + '\n\n'
            f'You have a {seconds}-second deadline for environment preparation, execution, repair and waiting. '
            'Before ending, wait for every required command you started to reach a terminal state and inspect its result. '
            'Starting a background build or preparing the toolchain does not complete the task. '
            'The evaluator scores your actual execution evidence; it will not run missing commands or finish jobs for you. '
            'Do not replace a required tool version, skip tests, or weaken checks. '
            'If setup cannot be completed, preserve the evidence and explain the unresolved cause.')


def recorder_hint_options(step, hints):
    """Apply optional paths only where the frozen launcher permits them.

    The recorder still probes actual versions and validates wrapper inputs.
    An irrelevant or malformed hint is diagnostic, never execution authority.
    """
    if not isinstance(hints, dict) or not isinstance(hints.get(step['id'], {}), dict):
        return [], {'_shape': {'status': 'ignored', 'reason': 'expected_step_path_mapping'}}
    hint, options, audit = hints.get(step['id'], {}), [], {}
    for name, flag in [('java_home', '--java-home'), ('maven_bin', '--maven-bin')]:
        if name not in hint:
            continue
        value, reason = hint[name], None
        if name == 'maven_bin' and (step['runner'] != 'maven' or step['argv'][0] != 'mvn'):
            reason = 'frozen_launcher_is_not_literal_mvn'
        elif not isinstance(value, str) or '\x00' in value or not value.startswith('/'):
            reason = 'expected_absolute_container_path'
        audit[name] = {'status': 'ignored' if reason else 'applied', 'reason': reason}
        if not reason:
            options.extend([flag, value])
    return options, audit


def run(args):
    from scripts.benchmark_execution_evidence import require_capture_support
    require_capture_support(args.arm, qualification=getattr(args, 'native_capture_qualification', False))
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    prepared = args.project.resolve()
    task, spec = normalize_task(load_json(prepared/'task.json')), load_json(prepared/'requirements.json')
    validate_requirements(spec, task)
    if spec['annotation_completeness']['status'] != 'complete':
        raise ValueError('Task definition review must be complete before a live attempt')
    if shutil.disk_usage(ROOT).free < RESERVE:
        raise RuntimeError('Host archival disk reserve unavailable')
    image = json.loads(call(['docker', 'image', 'inspect', args.image]))[0]
    if image['Architecture'] != 'amd64':
        raise ValueError('This campaign explicitly freezes linux/amd64')
    run_id = args.run_id
    name = 'sag-bench-' + run_id
    checkout = '/workspace/'+task['repo'].split('/')[1] if args.arm=='sag' else '/workspace/project'
    token = secrets.token_urlsafe(32)
    env = dict(os.environ, BENCH_GATEWAY_TOKEN=token, LITELLM_LOCAL_MODEL_COST_MAP='True')
    protocol_identity=evaluation_identity(spec)
    if args.arm=='sag':protocol_identity['requirements_file_sha256']=digest(prepared/'requirements.json')
    authority = {'schema_version': 1, 'recorder': 'agent-execution-controller-v1', 'run_id': run_id,
                 'target_repo_sha': task['sha'], 'sanitized_config': {'evaluation_protocol': protocol_identity}}
    manifest = {'schema_version': 1, 'runner_sha256': digest(__file__), 'gateway_sha256': digest(ROOT/'scripts/benchmark_model_gateway.py'),
                'usage_reader_sha256': digest(ROOT/'scripts/benchmark_gateway_usage.py'), 'created_at': now(),
                'intervention_protocol': POLICY, 'projects': [{'run_key': run_id, 'repo': task['repo'], 'sha': task['sha'],
                                                               'evaluation_protocol': protocol_identity}],
                'arm': args.arm, 'model': 'gpt-5.4-mini', 'reasoning_effort': 'native SAG parameters; baseline tool actor none',
                'advisor': 'mini-high' if args.arm=='sag' and not args.no_advisor else 'off',
                'image': image['Id'], 'architecture': 'linux/amd64', 'cpus': 4, 'memory_bytes': MEMORY,
                'deadline_seconds': args.seconds, 'cache': 'fresh container; no shared dependency caches',
                'execution_policy': 'agent-execution-v1', 'execution_origin': 'agent',
                'independent_replay': 'disabled; never part of the primary score',
                'time_boundary': 'fresh container preparation through agent termination and evidence close; no post-agent builds',
                'input_digests': {p.name: digest(p) for p in (prepared/'task.json', prepared/'requirements.json')},
                'cleanup': 'none after agent execution', 'phase': args.phase}
    manifest['opencode_termination']='Native parent-session step_finish(reason=stop), otherwise process exit; CLI exit status retained separately'
    if getattr(args, 'maven_witness', False):
        manifest['maven_witness']='Common frozen image observer, active during agent execution; no model-visible hints; all overhead charged'
    if args.arm=='sag':
        from scripts.benchmark_sag_boundary import freeze_source
        frozen=freeze_source(ROOT,out/'sag-source')
        write_json(out/'sag-source-inventory.json',frozen)
        manifest['sag_source_snapshot_sha256']=frozen['snapshot_sha256']
        sag_config=sag_model_config(load_json(args.sag_config), image=image['Id'],
                                   seconds=args.seconds, no_advisor=args.no_advisor)
        manifest['sag_config']=sag_config
        manifest['advisor']='off' if sag_config['advisor_mode']=='off' else 'mini-high'
    write_json(out/'manifest.json', manifest)
    (out/'controller-source').mkdir()
    for source_name in ('run_portable_harness.py','benchmark_model_gateway.py','benchmark_gateway_usage.py','export_requirements_evaluator.py','benchmark_sag_boundary.py','benchmark_opencode_entrypoint.py','benchmark_execution_evidence.py','benchmark_native_observer.py','benchmark_native_capture.py','benchmark_native_hook.py','benchmark_opencode_observer.mjs'):
        shutil.copy2(ROOT/'scripts'/source_name,out/'controller-source'/source_name)
    write_json(out/'run-pin.json', authority)
    shutil.copytree(prepared, out/'inputs')
    export_runtime(ROOT, out/'recorder-runtime')
    recorder_hashes = {str(p.relative_to(out/'recorder-runtime')): digest(p) for p in (out/'recorder-runtime').rglob('*') if p.is_file()}
    write_json(out/'recorder-files.json', recorder_hashes)
    runtime_handoff = args.arm == 'sag' and sag_config.get('export_runtime_handoff', False)
    manifest['runtime_handoff'] = 'sag_receipt_export' if runtime_handoff else 'not_required_for_primary_scoring'
    write_json(out/'manifest.json', manifest)
    (out/'brief.txt').write_text(prompt(task, checkout, args.seconds, spec, runtime_handoff=runtime_handoff))
    intervention_protocol.initialize(out/'intervention-ledger', policy=POLICY, run_key=run_id, runner_file=Path(__file__), manifest=manifest)
    started_provision = time.monotonic()
    started_provision_at=now()
    container = None; gateway = None; agent = None; native_capture = None
    stop = threading.Event()
    result = {'run_key': run_id, 'run_id': run_id, 'authority_ok': False, 'evaluation_protocol': protocol_identity,
              'process_started': False, 'process_finished': False}

    def dex(argv, **kwargs):
        return call(['docker', 'exec', container, *argv], **kwargs)

    def recorder(mode, extra=(), timeout=60):
        dex(['env', 'PYTHONPATH=/opt/benchmark-recorder', 'python3', '-m', 'sag.benchmark.recorder', mode,
                    '--task', '/opt/benchmark-inputs/task.json', '--requirements', '/opt/benchmark-inputs/requirements.json',
                    '--repo-root', checkout, '--records', '/opt/benchmark-records', '--run-id', run_id, *extra], timeout=timeout)
        if mode != 'step':
            return dex(['cat', '/opt/benchmark-records/' + ('run.json' if mode == 'close' else 'run-open.json')])
        return dex(['python3', '-c', 'import json,pathlib; p=pathlib.Path("/opt/benchmark-records"); s=json.loads((p/"run-open.json").read_text()); print((p/s["invocations"][-1]["path"]).read_text())'])

    def upload_recorder():
        for local, remote in [('recorder-runtime', '/opt/benchmark-recorder'), ('inputs', '/opt/benchmark-inputs')]:
            call(['docker', 'cp', str(out/local), container + ':' + remote], timeout=120)

    def monitor():
        while not stop.is_set():
            row = {'at': now(), 'host_free_bytes': shutil.disk_usage(ROOT).free}
            try:
                row['stats'] = json.loads(call(['docker', 'stats', '--no-stream', '--format', '{{json .}}', container], timeout=20))
                row['cgroup'] = dex(['sh', '-c', 'cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.peak /sys/fs/cgroup/memory.events'])
                row['df'] = dex(['df', '-Pk', '/workspace'])
                row['docker_free_bytes'] = int(row['df'].splitlines()[-1].split()[3])*1024
                if min(row['host_free_bytes'], row['docker_free_bytes']) < RESERVE:
                    row['guard_stop'] = 'disk_reserve'
                    write_json(out/'resource-guard.json', row)
                    call(['docker', 'stop', '--time', '10', container])
            except Exception as exc:
                row['error'] = type(exc).__name__
            with (out/'resources.jsonl').open('a') as f:
                f.write(json.dumps(row)+'\n')
            stop.wait(15)

    thread = None
    def provision_baseline():
        nonlocal container
        container = call(['docker', 'run', '-d', '--platform', 'linux/amd64', '--name', name,
                          '--label', 'sag.experiment.run='+run_id, '--cpus', '4', '--memory', str(MEMORY),
                          '--memory-swap', str(MEMORY), '--entrypoint', '/bin/bash',
                          image['Id'], '-c', 'while true; do sleep 30; done'])
        write_json(out/'container.json', {'id': container, 'name': name, 'image': image['Id']})
        dex(['mkdir', '-p', '/workspace', '/work'])
        repo = task['repo'] if task['repo'].startswith('https://') else 'https://github.com/'+task['repo']+'.git'
        dex(['git', 'clone', '--no-checkout', repo, checkout], timeout=900)
        dex(['git', '-C', checkout, 'checkout', '--detach', task['sha']], timeout=90)
        if dex(['git', '-C', checkout, 'rev-parse', 'HEAD']) != task['sha']:
            raise ValueError('Prepared checkout differs from frozen commit')
        upload_recorder()
        initial = json.loads(recorder('start', ['--agent', args.arm]))
        call(['docker', 'cp', container+':/opt/benchmark-records', str(out/'initial-records')])
        if initial['admission'] != 'ready':
            raise ValueError('Initial checkout integrity is not established')
        # Remove controller-owned data before the agent can use any tool.
        dex(['rm', '-rf', '/opt/benchmark-recorder', '/opt/benchmark-inputs', '/opt/benchmark-records'])
        boundary = dex(['python3', '-c', 'import os,json; print(json.dumps({p:os.path.exists(p) for p in '+repr([str(ROOT),str(out),'/opt/benchmark-inputs','/opt/benchmark-records','/var/run/docker.sock'])+'}))'])
        write_json(out/'read-boundary-probe.json', json.loads(boundary))
        if any(json.loads(boundary).values()):
            raise ValueError('Controller files or host Docker socket are exposed')

    previous_handlers={}
    def cancel(signum, frame):
        result['cancelled']=True
        raise KeyboardInterrupt('Controller received signal '+str(signum))
    for sig in (signal.SIGINT,signal.SIGTERM):
        previous_handlers[sig]=signal.signal(sig,cancel)

    try:
        if args.arm!='sag':
            provision_baseline()
            from scripts.benchmark_native_capture import DockerCapture, claude_settings, CLIENT_VERSIONS
            if dex([args.arm, '--version']) != CLIENT_VERSIONS[args.arm]:
                raise ValueError('Native hook schemas are not qualified for this client version')
            policy = json.loads(dex(['cat', '/opt/sag-maven-witness/policy.json']))
            if policy != {k:v for k,v in spec.get('maven_observer', {}).items() if k!='policy'}:
                raise ValueError('Passive observer image differs from frozen measurement policy')
            native_capture = DockerCapture(container=container,arm=args.arm,output=out,run_id=run_id,
                checkout=checkout,task=task,spec=spec,port=args.port+1)
            for source, remote in [('benchmark_native_hook.py','/opt/benchmark-native-hook.py'),
                                   ('benchmark_opencode_observer.mjs','/opt/benchmark-opencode-observer.mjs')]:
                call(['docker','cp',str(out/'controller-source'/source),container+':'+remote])
            write_json(out/'claude-observer-settings.json',claude_settings())
            call(['docker','cp',str(out/'claude-observer-settings.json'),container+':/work/claude-observer-settings.json'])
            manifest['native_tool_observation'] = {'policy':'native-tool-hooks-v1','qualification':True,
                'settings_scope':'fresh experiment container only','native_tools_unchanged':True}
            write_json(out/'manifest.json',manifest)
        else:
            # The normal SAG entry point must create its own fresh container.
            # Its clone callback, rather than a late external probe, records task_start.
            if json.loads(call(['docker','ps','-a','--filter','name=^/'+name+'$','--format','{{json .}}']) or 'null'):
                raise ValueError('SAG attempt container already exists')
            container=name
            (out/'sag-native').mkdir()
        with (out/'gateway.log').open('w') as log:
            gateway = subprocess.Popen([sys.executable, str(ROOT/'scripts/benchmark_model_gateway.py'), '--directory', str(out/'gateway'),
                                        '--run-id', run_id, '--arm', args.arm, '--port', str(args.port)],
                                       env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{args.port}/health', timeout=1).close(); break
            except OSError:
                if gateway.poll() is not None: raise RuntimeError('Model gateway did not start')
                time.sleep(.1)
        endpoint = f'http://host.docker.internal:{args.port}'
        if args.arm!='sag':
            dex(['curl', '-fsS', '--max-time', '10', endpoint+'/health'])
            call(['docker','cp',str(out/'brief.txt'),container+':/work/brief.txt'])
        config = {'permission': {'*':'allow','webfetch':'deny','websearch':'deny','question':'deny','task':'deny'},
                  'model':'benchmark/gpt-5.4-mini', 'small_model':'benchmark/gpt-5.4-mini',
                  'enabled_providers':['benchmark'], 'autoupdate':False, 'share':'disabled',
                  'provider':{'benchmark':{'npm':'@ai-sdk/openai','name':'Frozen mini experiment','options':{'baseURL':endpoint+'/v1','apiKey':'{env:BENCH_GATEWAY_TOKEN}'},
                              'models':{'gpt-5.4-mini':{'name':'gpt-5.4-mini','limit':{'context':400000,'output':128000},'options':{'reasoningEffort':'none'}}}}}}
        write_json(out/'opencode.json',config)
        if args.arm=='opencode':
            config['plugin']=['file:///opt/benchmark-opencode-observer.mjs']
            write_json(out/'opencode.json',config)
        if args.arm!='sag':call(['docker','cp',str(out/'opencode.json'),container+':/work/opencode.json'])
        model_env = {'BENCH_GATEWAY_TOKEN':token,'OPENAI_API_KEY':token,'ANTHROPIC_AUTH_TOKEN':token,
                     'ANTHROPIC_API_KEY':token,'ANTHROPIC_BASE_URL':endpoint,'ANTHROPIC_MODEL':'gpt-5.4-mini',
                     'ANTHROPIC_DEFAULT_OPUS_MODEL':'gpt-5.4-mini','ANTHROPIC_DEFAULT_SONNET_MODEL':'gpt-5.4-mini',
                     'ANTHROPIC_DEFAULT_HAIKU_MODEL':'gpt-5.4-mini','CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1',
                     'DISABLE_AUTOUPDATER':'1','DISABLE_TELEMETRY':'1','OPENCODE_CONFIG':'/work/opencode.json',
                     'OPENCODE_DISABLE_AUTOUPDATE':'true','OPENCODE_DISABLE_SHARE':'true'}
        child_env = dict(os.environ, **model_env)
        if native_capture:
            model_env.update(native_capture.environment)
            child_env.update(native_capture.environment)
        exec_env = sum((['--env',key] for key in model_env),[])
        if args.arm=='sag':
            from scripts.d3r2_campaign import config_environment
            child_env={**os.environ,**config_environment(sag_config),'OPENAI_API_KEY':token,
                       'OPENAI_BASE_URL':f'http://127.0.0.1:{args.port}/v1','OPENAI_API_BASE':f'http://127.0.0.1:{args.port}/v1',
                       'PYTHONPATH':str(out/'sag-source/src'),'PYTHONUNBUFFERED':'1','SAG_GIT_SHA':frozen['base_commit'],
                       'LITELLM_LOCAL_MODEL_COST_MAP':'True','SAG_RUN_ORDER_INDEX':'0',
                       'SAG_DEPENDENCY_CACHE_STATE':manifest['cache']}
            write_json(out/'effective-sag-config.json',
                       effective_sag_config(sag_config, child_env, out/'sag-native'))
            cli=[sys.executable,'-m','sag.main','project','https://github.com/'+task['repo']+'.git',
                 '--ref',task['sha'],'--name',name.removeprefix('sag-'),'--record','--goal',(out/'brief.txt').read_text(),
                 '--acceptance-task-file',str(out/'inputs/task.json'),'--requirements-file',str(out/'inputs/requirements.json'),
                 '--ci-target-file',str(out/'inputs/ci-target.json')]
            agent_command=cli
            model_env={'OPENAI_BASE_URL':child_env['OPENAI_BASE_URL'],'OPENAI_API_KEY':token}
        elif args.arm == 'claude':
            cli = ['claude','-p',(out/'brief.txt').read_text(),'--model','gpt-5.4-mini','--output-format','stream-json','--verbose',
                   '--permission-mode','dontAsk','--allowedTools','Bash,TaskOutput,Read,Write,Edit,Glob,Grep','--tools','Bash,TaskOutput,Read,Write,Edit,Glob,Grep',
                   '--strict-mcp-config','--mcp-config','{"mcpServers":{}}','--setting-sources','',
                   '--settings','/work/claude-observer-settings.json']
        else:
            cli = ['opencode','run','--model','benchmark/gpt-5.4-mini','--format','json',(out/'brief.txt').read_text()]
        if args.arm!='sag':agent_command=['docker','exec',*exec_env,'--workdir',checkout,container,'setsid','--wait',*cli]
        if args.arm=='opencode':
            call(['docker','cp',str(out/'controller-source/benchmark_opencode_entrypoint.py'),container+':/opt/opencode-experiment-entrypoint.py'])
            agent_command=['docker','exec',*exec_env,'--workdir',checkout,container,'python3','/opt/opencode-experiment-entrypoint.py','--',*cli]
        write_json(out/'launch.json', {'argv':cli, 'environment':{k: ('<per-attempt gateway credential>' if k.endswith(('TOKEN','KEY')) else v) for k,v in model_env.items()}})
        write_json(out/'provisioning.json', {'seconds':time.monotonic()-started_provision,'finished_at':now(),
                                           'client_version':frozen['snapshot_sha256'] if args.arm=='sag' else dex([args.arm if args.arm=='opencode' else 'claude','--version'])})
        thread = threading.Thread(target=monitor,daemon=True);thread.start()
        begin=started_provision;deadline=begin+args.seconds
        agent_begin=time.monotonic()
        result['process_started_at']=started_provision_at
        result['inference_started_at']=now()
        with (out/'agent.jsonl').open('w') as log:
            agent=subprocess.Popen(agent_command,env=child_env,cwd=out/'sag-native' if args.arm=='sag' else ROOT,
                                   stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            result.update(process_id=os.getpid(),agent_process_id=agent.pid,process_started=True)
            write_json(out/'started.json',result)
            try:
                agent.wait(timeout=max(.001,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                result['outer_timeout']=True
                call(['docker','stop','--time','10',container])
                if agent.poll() is None:os.killpg(agent.pid,signal.SIGTERM)
                agent.wait(timeout=30)
        result['agent_exit_code']=agent.returncode
        result['agent_seconds']=time.monotonic()-agent_begin
        result['inference_finished_at']=now()
        if native_capture:
            native_capture.close(out/'agent.jsonl')
            native_capture=None
        if args.arm=='sag':
            # Preserve the prospective source boundary. Primary scoring uses
            # native execution evidence, never a post-agent build.
            from scripts.benchmark_sag_boundary import initial_from_sag
            sessions=list((out/'sag-native/logs').glob('session_*'))
            if len(sessions)!=1:raise ValueError('SAG session identity unavailable')
            initial,pin=initial_from_sag(sessions[0],out/'initial-records',task,spec,image_id=image['Id'])
            actual_config=pin.get('sanitized_config',{})
            changed=sorted(k for k,v in sag_config.items() if actual_config.get(k)!=v)
            if changed:raise ValueError(f'Native SAG configuration differs from prepared settings: {changed}')
            run_id=pin['run_id'];result['run_id']=run_id
            write_json(out/'run-pin.json',pin)
            info=json.loads(call(['docker','inspect',container]))[0]
            if info['Image']!=image['Id']:raise ValueError('SAG container image drift')
            container=info['Id']
            write_json(out/'container.json',{'id':container,'name':name,'image':info['Image']})
            if not info['State']['Running'] and not result.get('outer_timeout'):call(['docker','start',container])
        # Restore trusted recorder bytes only after model-driven tools stop.
        # On deadline, restarting the stopped container is for Git evidence
        # closure only: no build or inference is allowed to resume.
        if result.get('outer_timeout'):
            call(['docker','start',container])
        if (out/'initial-records').is_dir():
            upload_recorder()
            call(['docker','cp',str(out/'initial-records'),container+':/opt/benchmark-records'])
            if getattr(args, 'maven_witness', False):
                try:
                    call(['docker','cp',container+':/opt/sag-maven-observations',str(out/'agent-native-observations')],timeout=120)
                except Exception as exc:
                    # Preserve this boundary even if the actor never invoked Maven.
                    write_json(out/'agent-native-observations-unavailable.json',{'reason':type(exc).__name__})
            # Read the agent-exit filesystem without creating build outputs.
            # This inventory is descriptive, never a success shortcut.
            try:
                dex(['env','PYTHONPATH=/opt/benchmark-recorder','python3','-m',
                     'sag.benchmark.artifact_inventory','--requirements','/opt/benchmark-inputs/requirements.json',
                     '--root',checkout,'--run-id',run_id,'--boundary','agent_exit_before_acceptance',
                     '--output','/opt/benchmark-agent-outputs.json'], timeout=120)
                call(['docker','cp',container+':/opt/benchmark-agent-outputs.json',str(out/'agent-output-inventory.json')])
            except Exception as exc:
                write_json(out/'agent-output-inventory.json',{
                    'run_id':run_id,'boundary':'agent_exit_before_acceptance','status':'unavailable',
                    'error_type':type(exc).__name__,'counts':None})
            # Only close source observations. There is deliberately no recorder
            # step loop here: the evaluator must never complete an agent's work.
            recorder('close')
            call(['docker','cp',container+':/opt/benchmark-records',str(out/'boundary-records')],timeout=120)
        result['process_finished_at']=now();result['process_seconds']=time.monotonic()-begin
        result['process_finished']=True
        gateway.terminate();gateway.wait(timeout=45);gateway=None
        # Billing is observed independently of whether result scoring succeeds.
        usage=read_usage(out/'gateway',args.run_id,inference_started_at=result['inference_started_at'],
                         inference_finished_at=result['inference_finished_at'])
        result.update(known_tokens=usage['known_tokens'],
                      total_tokens=usage['known_tokens'] if usage['model_calls_complete'] else None)
        intervention_protocol.close(out/'intervention-ledger',result,process_started=True,process_finished=True)
        from scripts.benchmark_execution_evidence import agent_evidence
        try:
            records,record=agent_evidence(out,args.arm,task,spec,run_id)
        except (ValueError,KeyError,OSError) as exc:
            # Missing instrumentation is not a build failure and cannot trigger
            # a substitute execution. Preserve an explicit unmeasurable result.
            result['execution_evidence_error']=str(exc)
            records=out/'boundary-records'
            record=load_json(records/'run.json') if (records/'run.json').exists() else {
                'run_id':run_id,'repo':task['repo'],'commit':task['sha'],
                'evaluation_identity':evaluation_identity(spec),'invocations':[],'worktree':[]}
            record={**record,'execution_origin':'agent','agent':args.arm,'invocations':[]}
        if record is not None:
            metadata=records/'controller-evaluation'
            metadata.mkdir(parents=True,exist_ok=False)
            shutil.copy2(out/'run-pin.json',metadata/'run-pin.json')
            record['run_pin']=reference(records,metadata/'run-pin.json')
            result['authority_ok']=record['run_id']==run_id and record['commit']==task['sha']
            observed=intervention_protocol.copy_evidence(out/'intervention-ledger',metadata/'intervention-ledger',base=records,result=result,run=record)
            telemetry={**observed,**usage,'unattended_seconds':result['process_seconds']}
            write_json(metadata/'telemetry.json',telemetry)
            record['telemetry']=reference(records,metadata/'telemetry.json')
            write_json(metadata/'campaign-run.json',record)
            write_json(out/'primary-evidence.json',{'execution_policy':'agent-execution-v1',
                'base':str(records.relative_to(out)), 'run':reference(records,metadata/'campaign-run.json')})
            from sag.benchmark.evaluator import evaluate
            score=evaluate(task,spec,record,records,ci_source_base=out/'inputs',required_execution_origin='agent')
            write_json(out/'score.json',score)
            result.update(status=score['status'],total_tokens=score['total_tokens'],known_tokens=score['known_tokens'])
    except BaseException as exc:
        result['runner_error']=type(exc).__name__+': '+str(exc)
        if isinstance(exc,KeyboardInterrupt):result['cancelled']=True
    finally:
        if native_capture:
            native_capture.stop()
        if agent is not None and agent.poll() is None:
            os.killpg(agent.pid,signal.SIGTERM)
            agent.wait(timeout=30)
        if gateway is not None:
            gateway.terminate();gateway.wait(timeout=45)
        stop.set()
        if thread:thread.join(timeout=30)
        if container:
            try:
                if args.arm=='sag':
                    # Preserve the Actor-readable store and phase handoffs,
                    # not only host-side turn-record references. This is a
                    # post-inference copy; it performs no setup work.
                    running=json.loads(call(['docker','inspect',container]))[0]['State']['Running']
                    if running:call(['docker','stop','--time','10',container])
                    call(['docker','cp',container+':/workspace/.setup_agent',
                          str(out/'sag-container-evidence')],timeout=120)
                elif args.arm=='claude':
                    call(['docker','cp',container+':/root/.claude/projects',str(out/'client-project-transcripts')],timeout=60)
                elif args.arm=='opencode':
                    call(['docker','cp',container+':/root/.local/share/opencode',str(out/'client-state')],timeout=60)
            except Exception as exc:result['client_archive_error']=type(exc).__name__
            try:
                info=json.loads(call(['docker','inspect',container]))[0]
                write_json(out/'container-final.json',{k:info[k] for k in ('Id','Image','State','HostConfig','Mounts')})
                if info['State']['Running']:call(['docker','stop','--time','10',container])
            except Exception as exc:result['container_close_error']=str(exc)
        if not (out/'intervention-ledger/close.json').exists():
            result.setdefault('process_finished_at',now())
            intervention_protocol.close(out/'intervention-ledger',result,process_started=result['process_started'],process_finished=result['process_finished'])
        write_json(out/'result.json',result)
        write_json(out/'checksums.json',{str(p.relative_to(out)):digest(p) for p in out.rglob('*') if p.is_file() and p.name!='checksums.json'})
        for sig,handler in previous_handlers.items():signal.signal(sig,handler)
    print(json.dumps(result))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--project',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--arm',choices=['sag','claude','opencode'],required=True)
    parser.add_argument('--sag-config',type=Path,default=ROOT/'output/requirements20-mini-20260923/qualification/protocol.json')
    parser.add_argument('--no-advisor',action='store_true')
    parser.add_argument('--native-capture-qualification',action='store_true',
                        help='Opt in to bounded passive native-hook qualification; not a formal campaign')
    parser.add_argument('--image',required=True)
    parser.add_argument('--seconds',type=int,default=7200)
    parser.add_argument('--port',type=int,default=18571)
    parser.add_argument('--phase',default='instrumentation-qualification')
    parser.add_argument('--maven-witness',action='store_true')
    run(parser.parse_args())


if __name__=='__main__':
    main()

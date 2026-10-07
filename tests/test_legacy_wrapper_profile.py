import hashlib,json,os,subprocess
from copy import deepcopy
from pathlib import Path
import pytest
from sag.benchmark.wrapper_review import make_launcher_review,capture_wrapper_inputs,capture_request,snapshot_inputs,validate_launcher_review,LEGACY_JAR_STATE
from sag.benchmark.requirements import normalize_task

SOURCE=Path(__file__).parent/'fixtures/maven-wrapper-jar-3.2.0'

@pytest.fixture
def legacy(tmp_path):
 root=tmp_path/'repo';root.mkdir();files={'mvnw':(SOURCE/'mvnw').read_bytes(),'.mvn/wrapper/maven-wrapper.properties':(SOURCE/'maven-wrapper.properties').read_bytes()}
 for relative,raw in files.items():
  p=root/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
 (root/'mvnw').chmod(0o755)
 subprocess.run(['git','init','-q',str(root)],check=True);subprocess.run(['git','-C',str(root),'add','.'],check=True)
 subprocess.run(['git','-C',str(root),'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','fixture'],check=True)
 sha=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip();task=normalize_task({'repo':'arbitrary/legacy','sha':sha,'steps':[{'id':'s','runner':'maven','argv':['./mvnw','verify'],'cwd':'.','java_major':17,'maven_version':'3.9.4'}]})
 review=make_launcher_review(sha,files)
 env={k:'' for k in ('MAVEN_ARGS','MAVEN_CONFIG','MAVEN_PROJECTBASEDIR','MAVEN_BASEDIR','MVNW_REPOURL','MAVEN_DEBUG_OPTS','JAVACMD','MAVEN_OPTS','JAVA_TOOL_OPTIONS','_JAVA_OPTIONS','JDK_JAVA_OPTIONS')};env.update(MAVEN_USER_HOME=str(tmp_path/'m2'),MAVEN_SKIP_RC='true')
 return root,task,review,env

def snapshot(f,boundary='acceptance_before'):
 root,task,review,env=f;step=task['steps'][0];request=capture_request(task,step,review,root,'r','i',boundary,env)
 state=capture_wrapper_inputs(request)
 return state, lambda state: snapshot_inputs(review,task,step,state,'r','i',boundary)

def test_uses_production_profile():
 import sag.benchmark.wrapper_review as mod
 assert 'src/sag/benchmark' in mod.__file__

def test_downloaded_binary_is_pinned_but_not_a_tracked_input(legacy):
 before,verify=snapshot(legacy);a=verify(before);root=legacy[0];path=root/LEGACY_JAR_STATE['path'];path.write_bytes((SOURCE/'maven-wrapper-3.2.0.jar').read_bytes())
 after,verify_after=snapshot(legacy,'acceptance_after');assert verify_after(after)==a
 assert before['runtime_state'][0]['present'] is False and after['runtime_state'][0]['present'] is True
 assert after['runtime_state'][0]['sha256']==LEGACY_JAR_STATE['sha256']

@pytest.mark.parametrize('mutate',['binary','project_properties','user_properties','extra_input','symlink','source','environment'])
def test_changed_execution_inputs_never_certify(legacy,mutate,tmp_path):
 root,task,review,env=legacy
 if mutate=='binary':(root/LEGACY_JAR_STATE['path']).write_bytes(b'x'*LEGACY_JAR_STATE['bytes'])
 elif mutate=='project_properties':(root/'maven.properties').write_text('systemProp.skipTests=true')
 elif mutate=='user_properties':
  p=Path(env['MAVEN_USER_HOME'])/'maven.properties';p.parent.mkdir();p.write_text('systemProp.skipTests=true')
 elif mutate=='extra_input':(root/'.mvn/unknown.config').write_text('skipTests')
 elif mutate=='symlink':(root/LEGACY_JAR_STATE['path']).symlink_to(SOURCE/'maven-wrapper-3.2.0.jar')
 elif mutate=='source':(root/'mvnw').write_text((root/'mvnw').read_text()+'\n# changed\n')
 elif mutate=='environment':env['MAVEN_CONFIG']='-DskipTests'
 state,verify=snapshot(legacy)
 with pytest.raises(ValueError):verify(state)

@pytest.mark.parametrize('mutate',['source_hash','runtime_state_missing','binary_profile_changed','properties_proof_missing','properties_fake_absence'])
def test_offline_replay_rejects_forged_profile_or_omitted_observation(legacy,mutate):
 root,task,review,env=legacy
 if mutate in ('source_hash','runtime_state_missing','binary_profile_changed'):
  bad=deepcopy(review)
  if mutate=='source_hash':next(x for x in bad['files'] if x['path']=='mvnw')['sha256']='a'*64
  elif mutate=='runtime_state_missing':bad.pop('runtime_state')
  else:bad['runtime_state'][0]['sha256']='a'*64
  with pytest.raises(ValueError):validate_launcher_review(bad,task['sha'],task['steps'][0])
 else:
  state,verify=snapshot(legacy)
  if mutate=='properties_proof_missing':state['environment'].pop('legacy_system_properties')
  else:state['environment']['legacy_system_properties'][0]['absent']=None
  with pytest.raises(ValueError):verify(state)

@pytest.mark.parametrize('args',[['clean','install','-DskipTests'],['verify','-pl','!a,!b']])
def test_pinned_shell_forwards_argument_boundaries_unchanged(legacy,tmp_path,args):
 import sys
 root,_,_,env=legacy
 (root/LEGACY_JAR_STATE['path']).write_bytes((SOURCE/'maven-wrapper-3.2.0.jar').read_bytes())
 jdk=tmp_path/'fake-jdk';(jdk/'bin').mkdir(parents=True);java=jdk/'bin/java'
 java.write_text('#!'+sys.executable+'\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n');java.chmod(0o755)
 invocation=subprocess.run([str(root/'mvnw'),*args],cwd=root,env={**os.environ,**env,'JAVA_HOME':str(jdk)},capture_output=True,text=True,check=True)
 forwarded=json.loads(invocation.stdout)
 assert forwarded==['-classpath',str(root/LEGACY_JAR_STATE['path']),'-Dmaven.multiModuleProjectDirectory='+str(root),'org.apache.maven.wrapper.MavenWrapperMain',*args]

def test_legacy_develocity_state_has_independent_byte_evidence(legacy):
 root,task,review,env=legacy
 p=root/'.mvn/extensions.xml';p.write_text('<extensions><extension><groupId>com.gradle</groupId><artifactId>develocity-maven-extension</artifactId><version>1.22.2</version></extension></extensions>')
 subprocess.run(['git','-C',str(root),'add','.mvn/extensions.xml'],check=True);subprocess.run(['git','-C',str(root),'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','extension fixture'],check=True)
 task['sha']=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()
 paths=[r['path'] for r in review['files']]+['.mvn/extensions.xml'];review=make_launcher_review(task['sha'],{r:(root/r).read_bytes() for r in paths});f=(root,task,review,env)
 assert len(review['runtime_state'])==2
 before,verify=snapshot(f);a=verify(before)
 (root/LEGACY_JAR_STATE['path']).write_bytes((SOURCE/'maven-wrapper-3.2.0.jar').read_bytes())
 p=root/'.mvn/.develocity/develocity-workspace-id';p.parent.mkdir();p.write_bytes(b'a'*26)
 after,verify=snapshot(f,'acceptance_after');assert verify(after)==a
 assert after['runtime_state'][1]['present'] is True
 p.write_bytes(b'0'*26);invalid,verify=snapshot(f,'acceptance_after')
 with pytest.raises(ValueError):verify(invalid)


def test_legacy_binary_must_be_observed_at_execution_close(legacy):
 state,verify=snapshot(legacy,'acceptance_after')
 with pytest.raises(ValueError,match='reviewed launcher binary'):verify(state)

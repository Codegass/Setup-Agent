"""An empty native banner alone never establishes empty test selection."""
from copy import deepcopy
import hashlib
import io
import json
import xml.etree.ElementTree as ET
import zipfile

import pytest

from scripts.build_benchmark_requirements import reviewed_empty_surefire_selection


@pytest.fixture
def reviewed(tmp_path):
    def save(name, data):
        raw=data if isinstance(data,bytes) else json.dumps(data).encode()
        path=tmp_path/name;path.write_bytes(raw)
        return {'path':name,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
    commit='a'*40
    source=b'package p; public class ExampleIT { interface TestAction {} }\n'
    blob=hashlib.sha1(b'blob '+str(len(source)).encode()+b'\0'+source).hexdigest()
    tree={'sha':commit,'truncated':False,'tree':[{'path':'m/src/test/java/p/ExampleIT.java','mode':'100644','type':'blob','sha':blob}]}
    model=ET.fromstring('''<project><build><testSourceDirectory>/w/m/src/test/java</testSourceDirectory><testOutputDirectory>/w/m/target/test-classes</testOutputDirectory><plugins><plugin><artifactId>maven-surefire-plugin</artifactId><version>3.5.6</version></plugin><plugin><artifactId>maven-compiler-plugin</artifactId><version>3.15.0</version></plugin></plugins></build></project>''')
    jar=io.BytesIO()
    with zipfile.ZipFile(jar,'w') as z:
        z.writestr('org/apache/maven/plugin/surefire/SurefireMojo.java','protected String[] getDefaultIncludes() { return new String[] {"**/Test*.java", "**/*Test.java", "**/*Tests.java", "**/*TestCase.java"}; }')
        z.writestr('META-INF/maven/org.apache.maven.plugins/maven-surefire-plugin/pom.properties','version=3.5.6\n')
    log=b'[INFO] --- compiler:3.15.0:testCompile (default-testCompile) @ m ---\n[INFO] Compiling 1 source file with javac [debug release 11] to target/test-classes\n[INFO] --- surefire:3.5.6:test (default-test) @ m ---\n[INFO] BUILD SUCCESS\n'
    witness={'effective_pom_sha256':'b'*64,'reviewed_by':'test reviewer','reason':'Full source selection review',
        'git_tree':save('tree.json',tree),'git_commit':save('commit.json',{'sha':commit}),
        'surefire_sources':save('sources.jar',jar.getvalue()),'official_ci':save('ci.log',log),
        'test_sources':[{**save('ExampleIT.java',source),'source_path':'m/src/test/java/p/ExampleIT.java',
                         'declared_types':['ExampleIT','TestAction'],'reviewed_binary_names':['ExampleIT','ExampleIT$TestAction']}],
        'compiler_discovery_review':{'reason':'No compiler plugins or generators in this fixture',
            'effective_plugin_sha256':hashlib.sha256(ET.tostring(model.find('build/plugins/plugin[artifactId="maven-compiler-plugin"]'))).hexdigest()}}
    binding={'goal':'surefire:test','module':'m','version':'3.5.6','position':1,'line':3}
    task={'sha':commit,'steps':[{'argv':['mvn','verify']}]}
    return tmp_path,save,binding,model,witness,task


def test_empty_selection_requires_all_source_and_native_evidence(reviewed):
    base,_,b,m,w,t=reviewed
    reviewed_empty_surefire_selection(b,m,w,base,t,'/w')


@pytest.mark.parametrize('mutation',['new_source','truncated_tree','wrong_commit','source_tamper','custom_include','selector','new_generator','compilation_unknown','selected_class','compiler_changed','plugin_version','custom_resources','resource_copy','execution_test_include','hoisted_test_type'])
def test_empty_selection_cannot_hide_unknown_or_selected_tests(reviewed,mutation):
    base,save,b,m,w,t=reviewed
    if mutation in {'new_source','truncated_tree','wrong_commit'}:
        tree=json.loads((base/'tree.json').read_text())
        if mutation=='new_source':tree['tree'].append({'path':'m/src/test/java/p/RealTest.java','mode':'100644','type':'blob','sha':'f'*40})
        elif mutation=='truncated_tree':tree['truncated']=True
        else:tree['sha']='f'*40
        w['git_tree']=save('tree.json',tree)
    elif mutation=='source_tamper':(base/'ExampleIT.java').write_text('class HiddenTest {}')
    elif mutation=='custom_include':ET.SubElement(m.find('build/plugins/plugin'),'configuration')
    elif mutation=='selector':t['steps'][0]['argv'].append('-Dtest=ExampleIT')
    elif mutation=='new_generator':
        raw=(base/'ci.log').read_bytes().replace(b'[INFO] --- compiler',b'[INFO] --- build-helper:3.6.1:add-test-source (extra) @ m ---\n[INFO] --- compiler')
        w['official_ci']=save('ci.log',raw);b['position']=2;b['line']=4
    elif mutation=='compilation_unknown':w['official_ci']=save('ci.log',(base/'ci.log').read_bytes().replace(b'Compiling 1 source file',b'Compiling 2 source files'))
    elif mutation in {'selected_class','hoisted_test_type'}:
        raw=(base/'ExampleIT.java').read_bytes()
        name='ExampleTest' if mutation=='selected_class' else 'ExampleIT'
        raw=raw.replace(b'ExampleIT',name.encode()) if mutation=='selected_class' else raw.replace(b'{ interface TestAction {} }',b'{} interface TestAction {}')
        ref=save('ExampleIT.java',raw)
        tree=json.loads((base/'tree.json').read_text());tree['tree'][0].update(path='m/src/test/java/p/'+name+'.java',sha=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest())
        w['git_tree']=save('tree.json',tree)
        w['test_sources']=[{**ref,'source_path':tree['tree'][0]['path'],'declared_types':[name,'TestAction'],'reviewed_binary_names':[name,name+'$TestAction']}]
    elif mutation=='compiler_changed':ET.SubElement(m.find('build/plugins/plugin[artifactId="maven-compiler-plugin"]'),'configuration')
    elif mutation=='custom_resources':
        resources=ET.SubElement(m.find('build'),'testResources');resource=ET.SubElement(resources,'testResource')
        ET.SubElement(resource,'directory').text='/w/m/extra-test-resources'
        tree=json.loads((base/'tree.json').read_text());tree['tree'].append({'path':'m/extra-test-resources/p/HiddenTest.class','type':'blob','mode':'100644','sha':'f'*40})
        w['git_tree']=save('tree.json',tree)
    elif mutation=='resource_copy':
        plugin=ET.SubElement(m.find('build/plugins'),'plugin');ET.SubElement(plugin,'artifactId').text='maven-resources-plugin'
        config=ET.SubElement(plugin,'configuration');ET.SubElement(config,'outputDirectory').text='/w/m/target/test-classes'
    elif mutation=='execution_test_include':
        compiler=m.find('build/plugins/plugin[artifactId="maven-compiler-plugin"]');ex=ET.SubElement(compiler,'executions');ex=ET.SubElement(ex,'execution');c=ET.SubElement(ex,'configuration');ET.SubElement(c,'testIncludes')
        w['compiler_discovery_review']['effective_plugin_sha256']=hashlib.sha256(ET.tostring(compiler)).hexdigest()
    else:b['version']='3.6.0'
    with pytest.raises(ValueError):reviewed_empty_surefire_selection(b,m,w,base,t,'/w')

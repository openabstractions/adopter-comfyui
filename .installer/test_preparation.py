"""Source/inert fixture checks; native fixture execution requires an explicit binary.

No Comfy host, installer, user configuration or interpreter is executed.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile
import json
import importlib.util
import shutil
import unittest
import sys
import os
import hashlib
import zipfile
import types
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from personfolders import setUpModule, tearDownModule  # noqa: F401

class BuilderInputTests(unittest.TestCase):
    def offline_native_imports(self, *, changed_sum=False):
        go=shutil.which('go')
        if not go:self.skipTest('Go executable is required for the offline module regression')
        proxy=self.root/'proxy';versions=proxy/'example.test/dependency/@v';versions.mkdir(parents=True)
        (versions/'v1.0.0.mod').write_text('module example.test/dependency\n\ngo 1.23\n',newline='\n')
        (versions/'v1.0.0.info').write_text('{"Version":"v1.0.0","Time":"2026-01-01T00:00:00Z"}')
        with zipfile.ZipFile(versions/'v1.0.0.zip','w') as archive:
            archive.writestr('example.test/dependency@v1.0.0/go.mod',(versions/'v1.0.0.mod').read_bytes())
            archive.writestr('example.test/dependency@v1.0.0/value.go','package dependency\n')
        stage=self.root/'native-copy';stage.mkdir()
        (stage/'go.mod').write_text('module example.test/app\n\ngo 1.23\n\nrequire example.test/dependency v1.0.0\n',newline='\n')
        (stage/'main.go').write_text('package main\nimport _ "example.test/dependency"\nfunc main() {}\n',newline='\n')
        bad='example.test/dependency v1.0.0 h1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n'
        if changed_sum:(stage/'go.sum').write_text(bad,newline='\n')
        environment=dict(os.environ,GOPROXY=proxy.as_uri(),GOSUMDB='off',GONOSUMDB='*',GOWORK='off',
            GOENV='off',GOTOOLCHAIN='local',GOTELEMETRY='off',GOFLAGS='-mod=readonly -modcacherw',
            GOMODCACHE=str(self.root/'gomod'),GOCACHE=str(self.root/'gocache'))
        run=subprocess.run;effects=[]
        class ResourceReached(Exception):pass
        def dispatch(argv,**kwargs):
            if argv[0]==go:
                return run(argv,**dict(kwargs,stderr=subprocess.PIPE))
            effects.append(argv)
            raise ResourceReached()
        with patch.object(self.build.subprocess,'run',side_effect=dispatch):
            arguments=(self.shared,stage,stage/'host.exe',{'go':go},environment,
                       dict(compilers={'rc':'inert-rc','cvtres':'inert-cvtres'}))
            if changed_sum:
                with self.assertRaises(subprocess.CalledProcessError) as failure:
                    self.build._compile_native_resources(*arguments)
                self.assertIn(b'checksum mismatch',failure.exception.stderr)
                self.assertEqual((stage/'go.sum').read_text(),bad)
                self.assertEqual(effects,[])
            else:
                with self.assertRaises(ResourceReached):self.build._compile_native_resources(*arguments)
                self.assertIn('example.test/dependency v1.0.0 h1:',(stage/'go.sum').read_text())
                self.assertEqual(len(effects),1)
            self.assertEqual(environment['GOFLAGS'],'-mod=readonly -modcacherw')

    def test_native_import_closure_resolves_missing_sums_from_offline_proxy(self):
        self.offline_native_imports()

    def test_native_import_closure_refuses_a_changed_existing_sum(self):
        self.offline_native_imports(changed_sum=True)

    def test_private_projection_check_skips_in_published_layout(self):
        public=self.root/'published/.installer/test_preparation.py'
        with patch.dict(globals(), __file__=str(public)):
            for name in ('test_resource_sanitizer_closure_matches_private_sources_and_public_projection',
                         'test_private_projection_check_skips_in_published_layout'):
                with self.subTest(name=name):
                    result=unittest.TestResult()
                    BuilderInputTests(name).run(result)
                    self.assertEqual(result.testsRun,1)
                    self.assertEqual(len(result.skipped),1)
                    self.assertEqual(result.errors,[])
                    self.assertEqual(result.failures,[])

    def test_resource_sanitizer_closure_matches_private_sources_and_public_projection(self):
        root=Path(__file__).resolve().parents[3]
        manifest=root/'scripts/split.manifest'
        if not manifest.is_file():
            self.skipTest('organization projection is private input; private gate runs this check')
        sources=self.build.backend_resource_tool_sources(root/'installer/apps')
        actual={path.name for path in (root/'monitor/icon/gen').glob('*.go') if not path.name.endswith('_test.go')}
        self.assertEqual({path.name for path in sources},actual)
        self.assertIn('agent_release.go',actual)
        projection=manifest.read_text(encoding='utf-8')
        for repository in ('abstractions','redist'):
            selected=projection.split('repo '+repository+'\n',1)[1].split('\nrepo ',1)[0]
            for path in sources:
                self.assertIn('file tools/windows-resource-gen/'+path.name+' monitor/icon/gen/'+path.name,selected)
        public=self.root/'public';shared=public/'windows/apps';shared.mkdir(parents=True)
        tools=public/'tools/windows-resource-gen';tools.mkdir(parents=True)
        for path in sources:(tools/path.name).write_bytes(path.read_bytes())
        selected=self.build.backend_resource_tool_sources(shared)
        self.assertEqual({path.name for path in selected},actual)
        (tools/'agent_release.go').unlink()
        with self.assertRaisesRegex(ValueError,'sanitizer source unavailable'):
            self.build.backend_resource_tool_sources(shared)

    def test_resource_sanitizer_agent_source_change_refuses_before_native_link(self):
        resources=self.root/'monitor/icon/gen';resources.mkdir(parents=True)
        for name in ('main.go','native.go','agent_release.go'):(resources/name).write_bytes(b'inert source')
        stage=self.out/'stage';stage.mkdir()
        calls=[]
        def run(argv,**kwargs):
            calls.append(argv)
            if argv[1]=='run':
                self.assertEqual({Path(value).name for value in argv[3:-2]},
                                 {'main.go','native.go','agent_release.go'})
                (resources/'agent_release.go').write_bytes(b'changed source')
            return subprocess.CompletedProcess(argv,0)
        with patch.object(self.build.subprocess,'run',side_effect=run):
            with self.assertRaisesRegex(ValueError,'sanitizer source changed'):
                self.build._compile_native_resources(self.shared,stage,stage/'host.exe',{'go':'fixture-go'},
                    {},dict(compilers={'rc':'fixture-rc','cvtres':'fixture-cvtres'}))
        self.assertFalse(any(argv[1]=='build' for argv in calls))

    def test_backend_build_owners_select_exact_private_and_public_layouts(self):
        root=self.root/'selected-source';shared=root/'installer/apps';shared.mkdir(parents=True)
        (root/'scripts').mkdir();(root/'tools').mkdir()
        (root/'CLAUDE.md').write_text('private source marker')
        (root/'scripts/split.manifest').write_text('private split mapping')
        self.assertEqual(self.build.backend_build_owner_paths(shared)['sdk'],root/'scripts/windows_go_sdk.py')
        # Public redist never falls back to the private source owner, even if
        # a similarly named file exists outside its selected tools directory.
        public=root/'windows/apps';public.mkdir(parents=True)
        self.assertEqual(self.build.backend_build_owner_paths(public)['sdk'],root/'tools/windows_go_sdk.py')
        (root/'scripts/split.manifest').unlink()
        self.assertEqual(self.build.backend_build_owner_paths(shared)['sdk'],root/'tools/windows_go_sdk.py')

    def test_missing_private_sdk_owner_refuses_without_public_or_ambient_rescue(self):
        role=self.root/'go-role';role.mkdir();entry,files=self.go_role(role)
        public=self.root/'tools';public.mkdir()
        (public/'windows_go_sdk.py').write_text('raise AssertionError("foreign public owner")')
        with patch.object(self.build.subprocess,'run',side_effect=AssertionError('no compiler')):
            with self.assertRaisesRegex(ValueError,'selected Windows Go build owner unavailable'):
                self.build.build_backend_launcher({'go':dict(root=role,entry=entry,files=files)},
                    self.shared,self.out,self.out/'resource')
        self.assertFalse((self.out/'backend-launch-build').exists())

    def go_role(self, tree):
        actual=Path(__file__).resolve().parents[3]/'openabstractions-flat/abstraction-provider-kit/go'
        members={'archive.zip':b'inert selected archive','sdk-receipt.json':b'{}',
                 'go/bin/go.exe':b'inert selected Go; never executed','proxy/fixture':b'inert prepared proxy member',
                 'module/go.mod':(actual/'go.mod').read_bytes(),'module/go.sum':(actual/'go.sum').read_bytes()}
        for name,data in members.items():
            target=tree/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
        return tree/'go/bin/go.exe',{name:self.build.pin(tree/name) for name in members}

    def test_launcher_admission_uses_actual_provider_module_before_any_build_effect(self):
        root=self.root/'go-role';root.mkdir()
        entry,files=self.go_role(root)
        tools={'go':dict(root=root,entry=entry,files=files)}
        with patch.object(self.build.subprocess,'run',side_effect=AssertionError('no compiler during admission')):
            module,source,source_pin=self.build.backend_launcher_inputs(tools)
            self.assertEqual(module,root/'module')
            self.assertEqual(source,self.build.HERE/'backendlaunch/main_windows.go')
            self.assertEqual(source_pin,self.build.pin(source))
            original=(module/'go.mod').read_bytes()
            for changed in (original.replace(b'abstraction-provider-kit/go',b'abstraction-provider-kit',1),
                            original.replace(b'abstraction-provider-kit/go',b'foreign/module',1),
                            original+b'\nreplace example.test/a => ../foreign\n'):
                with self.subTest(module=changed):
                    (module/'go.mod').write_bytes(changed)
                    with self.assertRaisesRegex(ValueError,'prepared provider-kit'):
                        self.build.build_backend_launcher(tools,self.shared,self.out,self.out/'resource')
            (module/'go.mod').write_bytes(original)
            source.unlink()
            with self.assertRaisesRegex(ValueError,'source-owned backend launcher'):
                self.build.backend_launcher_inputs(tools)
        self.assertFalse((self.out/'backend-launch-build').exists())

    def test_helper_compiler_refuses_incomplete_or_ambient_go_before_effects(self):
        root=self.root/'go-role';root.mkdir()
        required={'archive.zip','sdk-receipt.json','module/go.mod','module/go.sum','go/bin/go.exe','proxy/fixture'}
        for variant in ('missing','ambient','extra'):
            files={name:dict(bytes=1,sha256='1'*64) for name in required}
            entry=root/'go/bin/go.exe'
            if variant=='missing':files.pop('proxy/fixture')
            elif variant=='ambient':entry=Path('go.exe')
            else:files['foreign.exe']=dict(bytes=1,sha256='1'*64)
            with self.subTest(variant=variant),patch.object(self.build.subprocess,'run',side_effect=AssertionError('no compiler before admission')):
                with self.assertRaisesRegex(ValueError,'exact SDK'):
                    self.build.build_backend_launcher({'go':dict(root=root,entry=entry,files=files)},self.shared,self.out,self.out/'resource')

    def test_actual_launcher_command_uses_admitted_module_and_only_offline_role(self):
        self.check_launcher_command(False)

    def test_native_launcher_preserves_its_admitted_self_module_packages(self):
        self.check_launcher_command(True)

    def test_native_module_refusal_precedes_every_compiler(self):
        self.check_launcher_command(True,module_refused=True)

    def test_native_sdk_work_is_independent_of_deep_output(self):
        self.check_launcher_command(True,deep_output=True)

    def test_native_compiler_timeout_retains_exact_recovery_work(self):
        self.check_launcher_command(True,compiler_timeout=True)

    def test_private_native_workspace_uses_current_source_and_same_resource_owner(self):
        (self.root/'go.work').write_text('go 1.26.5\n',encoding='utf-8')
        module=self.root/'openabstractions-flat/abstraction-provider-kit/go'
        module.mkdir(parents=True)
        (module/'go.mod').write_text('module github.com/openabstractions/abstraction-provider-kit/go\n',encoding='utf-8')
        resources=self.root/'monitor/icon/gen';resources.mkdir(parents=True)
        for name in ('main.go','native.go','agent_release.go'):(resources/name).write_bytes(b'package main\n')
        sdk_path=self.root/'sdk.json';identity={'fixture':'selected SDK'}
        receipt=dict(go='fixture-go',source_identity=identity)
        sdk_path.write_text(json.dumps(receipt),encoding='utf-8')
        sdk=types.SimpleNamespace(validate_sdk_receipt=lambda value:self.assertEqual(value,receipt),
            sdk_environment=lambda value,environment:dict(environment))
        destination=self.out/'local-native';destination.mkdir()
        backend=self.root/'backend.json';backend.write_bytes(b'{}')
        context=dict(paths={'main_windows.go':self.build.HERE/'backendlaunch/main_windows.go'},
            inputs={'backend':dict(path=backend,pin=self.build.pin(backend))},
            compilers={'rc':'fixture-rc','cvtres':'fixture-cvtres'})
        calls=[]
        def run(argv,**kwargs):
            calls.append((argv,kwargs))
            environment=kwargs['env']
            self.assertEqual(environment['GOWORK'],str(self.root/'go.work'))
            self.assertEqual((environment['GOPROXY'],environment['GOFLAGS'],environment['CGO_ENABLED']),('off','-mod=readonly','0'))
            self.assertTrue(kwargs['cwd'].is_relative_to(module/'.forks'))
            self.assertEqual((kwargs['cwd']/'main_windows.go').read_bytes(),context['paths']['main_windows.go'].read_bytes())
            if argv[1]=='build':Path(argv[4]).write_bytes(b'inert native output')
            return subprocess.CompletedProcess(argv,0)
        source_identity=dict(source_commit='a'*40,compiler=identity)
        with patch.object(self.build,'private_native_source_identity',return_value=(source_identity,sdk)), \
                patch.object(self.build._boundary,'windows_build_environment',return_value={'PATH':'fixed-os'}), \
                patch.object(self.build.subprocess,'run',side_effect=run), \
                patch.dict(os.environ,{'GOCACHE':str(self.root/'cache'),'GOMODCACHE':str(self.root/'mods')}):
            result=self.build._build_local_native_host(self.shared,destination,context,self.root,sdk_path,'a'*40)
        self.assertEqual(len(calls),5)
        self.assertEqual(result['source_mode'],'private-local-preview')
        self.assertEqual(result['private_source_identity'],source_identity)
        self.assertEqual(result['native'],self.build.pin(destination/'launch-helper.exe'))
        self.assertFalse(calls[0][1]['cwd'].exists())

    def test_private_native_unknown_workspace_and_missing_cache_refuse_before_process(self):
        destination=self.out/'local-native';destination.mkdir()
        with patch.object(self.build.subprocess,'run',side_effect=AssertionError('physical process forbidden')):
            with self.assertRaisesRegex(ValueError,'exact source workspace'):
                self.build._build_local_native_host(self.shared,destination,{},self.root/'foreign',self.root/'absent.json','a'*40)
            (self.root/'go.work').write_text('go 1.26.5\n',encoding='utf-8')
            with patch.dict(os.environ,{},clear=True),self.assertRaisesRegex(ValueError,'admitted Go caches'):
                self.build._build_local_native_host(self.shared,destination,{},self.root,self.root/'absent.json','a'*40)

    def test_private_native_output_and_module_reparse_ancestry_refuse_before_process(self):
        (self.root/'go.work').write_text('go 1.26.5\n',encoding='utf-8')
        module=self.root/'openabstractions-flat/abstraction-provider-kit/go'
        module.mkdir(parents=True)
        (module/'go.mod').write_text('module fixture\n',encoding='utf-8')
        destination=self.out/'local-native';destination.mkdir()
        original=Path.lstat
        for target in (destination.parent,module.parent):
            selected=self.build._boundary.extended_path(target)
            def reparse(path,*args,**kwargs):
                if path==selected:return types.SimpleNamespace(st_mode=0o040000,st_file_attributes=0x400)
                return original(path,*args,**kwargs)
            receipt=self.root/'sdk.json';receipt.write_text('{}',encoding='utf-8')
            with self.subTest(target=target),patch.object(Path,'lstat',reparse), \
                    patch.object(self.build,'private_native_source_identity',return_value=({},None)), \
                    patch.dict(os.environ,GOCACHE='admitted',GOMODCACHE='admitted'), \
                    patch.object(self.build.subprocess,'run',side_effect=AssertionError('physical process forbidden')):
                with self.assertRaisesRegex(ValueError,'linked'):
                    self.build._build_local_native_host(self.shared,destination,{'compilers':{}},self.root,receipt,'a'*40)

    def test_private_native_source_identity_binds_clean_revision_sdk_and_current_inputs(self):
        (self.root/'go.work').write_text('go 1.26.5\n',encoding='utf-8')
        resource_sources=self.root/'monitor/icon/gen';resource_sources.mkdir(parents=True)
        for name in ('main.go','native.go','agent_release.go'):(resource_sources/name).write_bytes(b'inert resource source')
        sdk_path=self.root/'sdk.json'
        receipt=dict(receipt_path=str(sdk_path),source_identity={'selected':'SDK'})
        sdk_path.write_text(json.dumps(receipt),encoding='utf-8')
        compilers={name:self.root/(name+'.exe') for name in ('rc','cvtres')}
        for path in compilers.values():path.write_bytes(b'inert selected compiler')
        calls=[]
        def revision(root,selected):
            calls.append(('source',selected))
            self.assertEqual(root,self.root)
            if selected!='a'*40:raise ValueError('selected clean revision differs')
        source=types.SimpleNamespace(_require_private_source_revision=revision)
        sdk=types.SimpleNamespace(validate_sdk_receipt=lambda value:calls.append(('SDK',value)))
        def spec(name,path):
            selected=source if name=='comfy_private_source_revision_owner' else sdk
            self.assertIn(name,('comfy_private_source_revision_owner','comfy_local_windows_go_sdk'))
            loader=types.SimpleNamespace(create_module=lambda value:None,
                exec_module=lambda module:module.__dict__.update(vars(selected)))
            return importlib.util.spec_from_loader(name,loader)
        native={'main_windows.go':{'bytes':1,'sha256':'1'*64}}
        graph={'web/oa_appmodel.js':{'bytes':2,'sha256':'2'*64}}
        with patch.object(self.build.importlib.util,'spec_from_file_location',side_effect=spec), \
                patch.object(self.build,'native_host_source_inputs',return_value=({},native)), \
                patch.object(self.build,'native_graph_source_inputs',return_value=({},graph)), \
                patch.object(self.build.subprocess,'Popen',side_effect=AssertionError('physical process forbidden')):
            identity,_=self.build.private_native_source_identity(self.root,'a'*40,sdk_path,compilers)
            self.assertEqual(identity['source_commit'],'a'*40)
            self.assertEqual(identity['source_files'],native)
            self.assertEqual(identity['graph_sources'],graph)
            self.assertEqual(identity['resource_tools'],{name:self.build.pin(path) for name,path in compilers.items()})
            self.assertEqual(identity['resource_postprocess'],{path.name:self.build.pin(path)
                for path in self.build.backend_resource_tool_sources(self.shared)})
            self.assertEqual([row[0] for row in calls],['source','SDK'])
            original=Path.lstat
            ancestor=self.build._boundary.extended_path(self.root)
            def reparse(path,*args,**kwargs):
                if path==ancestor:return types.SimpleNamespace(st_mode=0o040000,st_file_attributes=0x400)
                return original(path,*args,**kwargs)
            with patch.object(Path,'lstat',reparse),self.assertRaisesRegex(ValueError,'linked'):
                self.build.private_native_source_identity(self.root,'a'*40,sdk_path,compilers)
            with self.assertRaisesRegex(ValueError,'clean revision'):
                self.build.private_native_source_identity(self.root,'b'*40,sdk_path,compilers)
            sdk_path.unlink()
            with self.assertRaises(FileNotFoundError):
                self.build.private_native_source_identity(self.root,'a'*40,sdk_path,compilers)

    def test_local_native_cli_refuses_public_run_identity(self):
        args=['--phase','local-native','--out',str(self.out/'fresh-local'),'--version','0.8.0',
            '--shared-app-builder',str(self.shared),'--source-root',str(self.root),
            '--private-sdk-receipt',str(self.root/'sdk.json'),'--backend-manifest',str(self.root/'backend.json'),
            '--private-source-commit','a'*40,
            '--control-manifest',str(self.root/'control.json'),'--cvtres',str(self.root/'cvtres.exe'),
            '--producer-run','123']
        with patch.object(self.build.subprocess,'run',side_effect=AssertionError('physical process forbidden')):
            with self.assertRaisesRegex(ValueError,'no public producer'):
                self.build.main(args)
        self.assertFalse((self.out/'fresh-local').exists())

    def test_launcher_output_alias_refuses_before_build_effects(self):
        root=self.root/'go-role';root.mkdir();entry,files=self.go_role(root)
        destination=self.out/'launcher';destination.mkdir()
        victim=self.root/'held-output';victim.write_bytes(b'held ordinary bytes')
        for name in ('launch-helper.exe','launch-helper-pin.json'):
            target=destination/name;os.link(victim,target)
            with self.subTest(name=name),patch.object(self.build.subprocess,'run',side_effect=AssertionError('no child')):
                with self.assertRaisesRegex(ValueError,'ordinary owned files'):
                    self.build.build_backend_launcher({'go':dict(root=root,entry=entry,files=files)},
                        self.shared,self.out,destination)
            self.assertEqual(victim.read_bytes(),b'held ordinary bytes')
            target.unlink()

    def check_launcher_command(self,native,*,module_refused=False,deep_output=False,compiler_timeout=False,real_private_root=False):
        root=self.root/'go-role';root.mkdir();entry,files=self.go_role(root)
        package=root/'module/enginelaunch/owner.go';package.parent.mkdir(parents=True)
        package.write_bytes(b'package enginelaunch\n// admitted self-module source\n')
        files['module/enginelaunch/owner.go']=self.build.pin(package)
        identity={'digest':'inert source identity'}
        (root/'sdk-receipt.json').write_text(json.dumps(dict(source_identity=identity)))
        files['sdk-receipt.json']=self.build.pin(root/'sdk-receipt.json')
        tools={'go':dict(root=root,entry=entry,files=files)}
        if deep_output:
            self.out=self.out/('deep-output-'*9)
            self.build._boundary.extended_path(self.out).mkdir(parents=True)
        destination=self.out/'launcher';destination.mkdir()
        if native:
            resource_tools=self.root/'monitor/icon/gen';resource_tools.mkdir(parents=True)
            for name in ('main.go','native.go','agent_release.go'):(resource_tools/name).write_bytes(b'package main\n')
        for path in self.build.backend_build_owner_paths(self.shared).values():
            path.write_bytes(b'# inert selected owner stand-in; never executed')
        def prepare(path,*,archive_path):
            self.assertEqual(archive_path,root/'archive.zip')
            if deep_output:
                # The official SDK contains this long ordinary testdata name.
                member='go/src/cmd/go/testdata/mod/github.com_dmitshur-test_modtest5_v0.5.0-alpha.0.20190619023908-3da23a9deb9e.txt'
                self.assertLess(len(str(path/'sdk-preparing-12345678'/member)),260)
            path.mkdir();compiler=path/'go.exe';compiler.write_bytes(entry.read_bytes())
            return dict(go=str(compiler),receipt_path=str(path/'receipt.json'),source_identity=identity)
        admitted=[]
        if real_private_root:
            path=Path(__file__).resolve().parents[3]/'scripts/windows_go_sdk.py'
            selected=importlib.util.spec_from_file_location('real_private_sdk_root',path)
            actual_sdk=importlib.util.module_from_spec(selected);selected.loader.exec_module(actual_sdk)
        def private(path,create):
            self.assertTrue(create)
            self.assertFalse(path.exists(),'SDK creation owner must receive a fresh root')
            if real_private_root:actual_sdk._private_root(path,create=create)
            else:path.mkdir()
            if not admitted:
                container=path.parent
                self.assertTrue(container.name.startswith('comfy-go-'))
                self.addCleanup(lambda: self.build._boundary.remove_tree(container) if container.exists() else None)
            admitted.append(path)
        sdk=types.SimpleNamespace(_private_root=private,prepare_sdk=prepare,
                                  validate_sdk_receipt=lambda receipt: self.assertEqual(receipt['source_identity'],identity))
        stage=types.SimpleNamespace(windows_sdk_environment=lambda path,env,sdk_owner: (env,dict(
            go=str(path/'go.exe'),receipt_path=str(path/'receipt.json'),source_identity=identity)))
        def spec(name,path):
            selected=sdk if name=='desktop_go_sdk' else stage
            self.assertIn(name,('desktop_go_sdk','desktop_go_stage'))
            loader=types.SimpleNamespace(create_module=lambda spec: None,
                exec_module=lambda module: module.__dict__.update(vars(selected)))
            return importlib.machinery.ModuleSpec(name,loader)
        calls=[];run=subprocess.run
        def execute(argv,**kwargs):
            if real_private_root and argv[0] in ('whoami','icacls'):
                return run(argv,**kwargs)
            calls.append((argv,kwargs))
            if argv[1]=='list':
                self.assertEqual(argv[1:],['list','-mod=mod','-deps','.'])
                self.assertEqual((kwargs['cwd']/'enginelaunch/owner.go').read_bytes(),package.read_bytes())
                self.assertEqual(kwargs['env']['GOFLAGS'],'-mod=readonly')
                if module_refused:
                    raise subprocess.CalledProcessError(1,argv,stderr='go: updates to go.mod needed, disabled by -mod=readonly')
                return subprocess.CompletedProcess(argv,0)
            if argv[0] in ('fixture-rc','fixture-cvtres'):
                return subprocess.CompletedProcess(argv,0)
            if argv[1]=='run':
                self.assertTrue(native)
                self.assertEqual(argv[-2],'-sanitize-coff')
                self.assertEqual([Path(value).name for value in argv[3:-2]],['main.go','native.go','agent_release.go'])
                return subprocess.CompletedProcess(argv,0)
            if native:
                self.assertEqual(kwargs['cwd'].name,'source')
                self.assertEqual((kwargs['cwd']/'enginelaunch/owner.go').read_bytes(),package.read_bytes())
                self.assertEqual(argv[-1],'.')
            else:
                self.assertEqual(kwargs['cwd'],root/'module')
                self.assertEqual(argv[-1],str(self.build.HERE/'backendlaunch/main_windows.go'))
            self.assertEqual(argv[1:4],['build','-trimpath','-o'])
            if compiler_timeout:raise subprocess.TimeoutExpired(argv,1)
            env=kwargs['env'];self.assertEqual(env['GOPROXY'],(root/'proxy').as_uri())
            self.assertEqual((env['GOWORK'],env['GOTOOLCHAIN'],env['GOFLAGS'],env['CGO_ENABLED']),
                             ('off','local','-mod=readonly','0'))
            Path(argv[4]).write_bytes(b'inert compiler output; never executed')
            return subprocess.CompletedProcess(argv,0)
        with patch.object(self.build.importlib.util,'spec_from_file_location',side_effect=spec), \
             patch.object(self.build._boundary,'windows_build_environment',return_value={'PATH':'fixed-os-path'}), \
             patch.object(self.build.subprocess,'run',side_effect=execute):
            backend=self.root/'backend.json';backend.write_bytes(b'{}')
            context=dict(paths={'main_windows.go':self.build.HERE/'backendlaunch/main_windows.go'},
                inputs={'backend':dict(path=backend,pin=self.build.pin(backend))},
                compilers={'rc':'fixture-rc','cvtres':'fixture-cvtres'}) if native else None
            if module_refused or compiler_timeout:
                with self.assertRaises(subprocess.CalledProcessError if module_refused else subprocess.TimeoutExpired) as failure:
                    self.build.build_backend_launcher(tools,self.shared,self.out,destination,native_context=context)
                self.assertEqual(calls[0][0][1],'list')
                self.assertFalse((destination/'launch-helper.exe').exists())
                work=calls[0][1]['cwd'].parent
                container=work.parent
                if module_refused:
                    self.assertEqual(len(calls),1)
                    self.assertFalse(work.exists())
                    self.assertFalse(container.exists())
                else:
                    self.assertTrue(work.is_dir())
                    self.assertEqual((work/'source/enginelaunch/owner.go').read_bytes(),package.read_bytes())
                    self.assertIn(str(container),'\n'.join(failure.exception.__notes__))
                    self.build._boundary.remove_tree(container)
                return
            result=self.build.build_backend_launcher(tools,self.shared,self.out,destination,native_context=context)
        self.assertEqual(len(calls),5 if native else 1)
        if native:self.assertEqual(set(result['resource_postprocess']),{'main.go','native.go','agent_release.go'})
        self.assertEqual(result['compiler'],identity)
        self.assertTrue((destination/'launch-helper-pin.json').is_file())
        self.assertFalse(Path(calls[-1][0][4]).parent.exists())
        self.assertFalse(admitted[0].parent.exists())

    @unittest.skipUnless(os.name=='nt','real Windows private-root admission')
    def test_launcher_sdk_creation_admits_fresh_root_through_real_owner_before_compile(self):
        self.check_launcher_command(True,real_private_root=True)

    def test_signed_helper_carrier_allows_only_exact_derived_member(self):
        build=self.build
        def measured(raw):return dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        unsigned=measured(b'unsigned helper');signed=measured(b'OA signed helper')
        before=measured(build.desktop_helper_carrier(unsigned))
        after=measured(build.desktop_helper_carrier(signed))
        original={build.BACKEND_HELPER:unsigned,build.BACKEND_HELPER_CARRIER:before,
            'desktop/resources/app.asar':measured(b'application')}
        receipt=dict(desktop_files=original,native_files={build.BACKEND_HELPER:signed},
            helper_carrier=dict(helper_unsigned=unsigned,helper_signed=signed,
                carrier_unsigned=before,carrier_signed=after))
        expected=build.desktop_payload_files(receipt)
        self.assertEqual(expected[build.BACKEND_HELPER_CARRIER],after)
        self.assertEqual(expected['desktop/resources/app.asar'],original['desktop/resources/app.asar'])
        for field in ('helper_unsigned','helper_signed','carrier_unsigned','carrier_signed'):
            changed=dict(receipt,helper_carrier=dict(receipt['helper_carrier']))
            changed['helper_carrier'][field]=measured(b'foreign')
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'carrier transform'):
                build.desktop_payload_files(changed)
        changed=dict(receipt,desktop_files=dict(original))
        changed['desktop_files'][build.BACKEND_HELPER_CARRIER]=after
        with self.assertRaisesRegex(ValueError,'original Desktop helper carrier'):
            build.desktop_payload_files(changed)

    def test_canonical_lf_policy_accepts_exact_bytes_and_refuses_old_pin_or_mutation(self):
        source=self.build.DESKTOP_SOURCE;data=source.read_bytes()
        self.assertNotIn(b'\r',data)
        self.assertEqual(self.policy['source_policy'],self.build.pin(source))
        self.assertEqual(self.build.custom_desktop_policy()['source_policy'],self.build.pin(source))
        policy_path=self.build.HERE/'desktop-host.json'
        old=dict(self.policy,source_policy=dict(bytes=len(data.replace(b'\n',b'\r\n')),
            sha256=hashlib.sha256(data.replace(b'\n',b'\r\n')).hexdigest()))
        policy_path.write_text(json.dumps(old))
        with self.assertRaisesRegex(ValueError,'identity/source policy differs'):
            self.build.custom_desktop_policy()
        policy_path.write_text(json.dumps(self.policy));source.write_bytes(data+b'\n')
        with self.assertRaisesRegex(ValueError,'identity/source policy differs'):
            self.build.custom_desktop_policy()

    def test_binding_identity_errors_and_helper_drift_refuse(self):
        with patch.object(self.build._boundary.os.path,'samefile',side_effect=PermissionError('identity denied')):
            with self.assertRaises(PermissionError):
                self.build.binding(self.file,self.input,historical=True)
        with patch.object(Path,'is_symlink',return_value=True):
            with self.assertRaisesRegex(ValueError,'plain source'):
                self.build.input_paths(None,self.shared,self.root)
        helper=Path(self.build.__file__).with_name('file_boundary.py')
        helper.write_bytes(helper.read_bytes()+b'\n# drift\n')
        with self.assertRaisesRegex(ValueError,'helper changed'):
            self.build.input_paths(None,self.shared,self.root)
        helper.unlink()
        spec=importlib.util.spec_from_file_location('missing_comfy_helper',self.build.__file__)
        module=importlib.util.module_from_spec(spec)
        with self.assertRaisesRegex(ValueError,'plain source'):
            exec(compile(Path(self.build.__file__).read_bytes(),self.build.__file__,'exec'),module.__dict__)
        shutil.copyfile(Path(__file__).with_name('file_boundary.py'),helper)
        exec(compile(Path(self.build.__file__).read_bytes(),self.build.__file__,'exec'),module.__dict__)
        self.assertEqual(module._boundary.relative_beneath_existing(self.file,self.input),Path('desktop-host.json'))

    def setUp(self):
        if self._testMethodName in ('test_resource_sanitizer_closure_matches_private_sources_and_public_projection',
                                    'test_private_projection_check_skips_in_published_layout'):
            manifest=Path(__file__).resolve().parents[3]/'scripts/split.manifest'
            if not manifest.is_file():
                self.skipTest('organization projection is private input; private gate runs this check')
        parent=Path(__file__).resolve().parents[3]/'.forks'
        parent.mkdir(parents=True,exist_ok=True)
        self.temporary=tempfile.TemporaryDirectory(prefix='comfy-builder-',dir=parent)
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name);source=self.root/'source';source.mkdir()
        actual=Path(__file__).resolve().parent
        for name in ('prepare_instance.c','hostfolder_action.c','instance_reader.h','prepare_instance.rc','prepare_instance.manifest','build_installer.py','file_boundary.py'):
            shutil.copyfile(actual/name,source/name)
        (source/'backendlaunch').mkdir()
        shutil.copyfile(actual/'backendlaunch/main_windows.go',source/'backendlaunch/main_windows.go')
        upstream=self.root/'upstream';upstream.mkdir()
        for name in ('desktop-source.json','desktop-oa.patch','desktop-nsis-sign.cjs','backend-amd-v1.json'):
            shutil.copyfile(actual.parent/'upstream'/name,upstream/name)
        (self.root/'desktop_backend.cjs').write_bytes(b'// inert fixture bridge source')
        spec=importlib.util.spec_from_file_location('fixture_comfy_builder',source/'build_installer.py')
        self.build=importlib.util.module_from_spec(spec)
        exec(compile((source/'build_installer.py').read_bytes(),str(source/'build_installer.py'),'exec'),self.build.__dict__)
        self.shared=self.root/'installer/apps';self.shared.mkdir(parents=True)
        (self.root/'scripts').mkdir();self.git_owner=self.root/'scripts/git_env.py'
        shutil.copyfile(actual.parents[2]/'scripts/git_env.py',self.git_owner)
        shutil.copyfile(actual.parents[2]/'scripts/app_pair_receipt.py',self.root/'scripts/app_pair_receipt.py')
        for name in self.build.SHARED:
            shutil.copyfile(actual.parents[2]/'installer/apps'/name,self.shared/name)
        for name in ('CLAUDE.md','scripts/split.manifest'):
            shutil.copyfile(actual.parents[2]/name,self.root/name)
        identity=self.root/'openabstractions-flat/abstraction-identity/fileid/native_windows.h';identity.parent.mkdir(parents=True)
        shutil.copyfile(actual.parents[2]/'openabstractions-flat/abstraction-identity/fileid/native_windows.h',identity)
        shutil.copyfile(actual.parents[2]/'installer/certum_simplysign.json',self.root/'certum_simplysign.json')
        shutil.copyfile(self.root/'certum_simplysign.json',self.shared.parent/'certum_simplysign.json')
        self.input=self.root/'inputs';self.input.mkdir();self.file=self.input/'desktop-host.json'
        self.policy=json.loads((actual/'desktop-host.json').read_bytes())
        # This class receives the retained Desktop variant, independently of
        # the current source-selected native archive route.
        self.policy['distribution']='oa-custom-desktop'
        # Inert fixture inputs; these are not authenticated build or signer evidence.
        self.policy['build_inputs']={name:dict(sha256='1'*64,bytes=1) for name in self.build.DESKTOP_BUILD_INPUTS}
        (source/'desktop-host.json').write_text(json.dumps(self.policy))
        self.file.write_text(json.dumps(self.policy))
        (self.input/'desktop').mkdir()
        exe=self.input/'desktop/OpenAbstractions ComfyUI.exe';exe.write_bytes(self.pe())
        resources=self.input/'desktop/resources';resources.mkdir()
        (resources/'app.asar').write_bytes(b'inert source-built Desktop JavaScript archive')
        installer=self.input/'OpenAbstractions-ComfyUI-1.1.8-win-x64.exe';installer.write_bytes(b'inert own installer fixture')
        pair=self.input/'comfyui-source.zip';pair.write_bytes(b'inert source-pair fixture; never qualified')
        cert=json.loads((self.root/'certum_simplysign.json').read_bytes())['certificate']
        def row(path):return dict(name=path.relative_to(self.input).as_posix(),**self.build.pin(path))
        self.declared=dict(schema=2,stage='desktop-final',version='0.8.0',source='a'*40,run_id=101,run_attempt=2,source_policy=self.policy['source_policy'],build_inputs=self.policy['build_inputs'],identity=self.policy['identity'],
            installer=row(installer),source_pair=row(pair),native_files={row(exe)['name']:self.build.pin(exe)},
            desktop_files={'desktop/'+entry['name']:{key:entry[key] for key in ('bytes','sha256')}
                for entry in self.build.desktop_native_inventory(self.input/'desktop',all_files=True)},
            host=dict(arp_id=self.policy['identity']['arp_id'],version='1.1.8',exe=row(exe),publisher=dict(subject=cert['subject'],thumbprint=cert['sha1'])))
        signatures=self.input/'desktop-signatures.json';signatures.write_bytes(b'{}')
        helpers={}
        for name in ('comfyui-hostfolder.dll','comfyui-prepare-instance.exe','desktop-uninstaller.exe'):
            member=self.input/name;member.write_bytes(b'inert signed helper fixture');helpers[name]=self.build.pin(member)
        helper_receipt=self.input/'preparation-native-receipt.json';helper_receipt.write_bytes(b'{}')
        self.declared.update(signatures=row(signatures),helpers=helpers,helper_receipt=row(helper_receipt))
        self.receipt=self.input/'desktop-build-receipt.json';self.received=self.root/'received.json';self.save()
        self.out=self.root/'out';self.out.mkdir()
        shutil.copytree(self.input/'desktop',self.out/'desktop')
        shutil.copyfile(signatures,self.out/signatures.name)
        signed={key:value for key,value in self.declared.items() if key not in ('installer','source_pair','helpers','helper_receipt')}
        signed.update(stage='desktop-inner-signed',input_receipt=self.build.pin(self.received),source_preparation=dict(bytes=1,sha256='2'*64))
        self.signed=self.out/'desktop-signed-receipt.json';self.signed.write_text(json.dumps(signed))
        def emit(args,out):
            out.mkdir(exist_ok=True)
            for suffix in ('a','b'):
                folder=out/('nsis-emit-'+suffix);folder.mkdir(exist_ok=True)
                (folder/'desktop-uninstaller-unsigned.exe').write_bytes(b'inert deterministic unsigned uninstaller')
            (out/'desktop-uninstaller.exe').write_bytes(b'inert deterministic unsigned uninstaller')
        self.addCleanup(patch.stopall)
        self.real_prepare_uninstaller=self.build.prepare_uninstaller
        patch.object(self.build,'prepare_uninstaller',side_effect=emit).start()
        self.argv=['--desktop-binding',str(self.file),'--input-root',str(self.input),'--shared-app-builder',str(self.shared),'--out',str(self.out),'--version','0.8.0','--cl','fixture-cl','--rc','fixture-rc','--input-receipt',str(self.received),'--signed-desktop-receipt',str(self.signed),'--signed-desktop-sha256',self.build.pin(self.signed)['sha256'],'--signed-desktop-bytes',str(self.build.pin(self.signed)['bytes']),'--producer-source','a'*40,'--producer-run','101','--producer-attempt','2']
    @staticmethod
    def pe():
        data=bytearray(128);data[:2]=b'MZ';data[60:64]=(64).to_bytes(4,'little');data[64:68]=b'PE\0\0';return data
    def save(self):
        self.receipt.write_text(json.dumps(self.declared))
        self.received.write_text(json.dumps(dict(build_inputs={'desktop-build-receipt.json':self.build.pin(self.receipt)})))

    def test_signed_desktop_refuses_changed_non_native_original_roster(self):
        measured=self.build.pin(self.signed)
        arguments=argparse.Namespace(signed_desktop_receipt=self.signed,signed_desktop_bytes=measured['bytes'],
            signed_desktop_sha256=measured['sha256'],producer_source='a'*40,producer_run=101,producer_attempt=2,version='0.8.0')
        self.build.signed_desktop(arguments,self.shared,self.out)
        resource=self.out/'desktop/resources/app.asar'
        original=resource.read_bytes();resource.write_bytes(b'foreign JavaScript with unchanged signed PE files')
        with self.assertRaisesRegex(ValueError,'complete file closure'):
            self.build.signed_desktop(arguments,self.shared,self.out)
        resource.write_bytes(original)
        extra=self.out/'desktop/resources/foreign.js';extra.write_bytes(b'foreign addition')
        with self.assertRaisesRegex(ValueError,'complete file closure'):
            self.build.signed_desktop(arguments,self.shared,self.out)
        extra.unlink();resource.unlink()
        with self.assertRaisesRegex(ValueError,'complete file closure'):
            self.build.signed_desktop(arguments,self.shared,self.out)

    def test_fixed_desktop_tools_stage_only_source_admitted_complete_members(self):
        root=self.input/'desktop-inputs';root.mkdir()
        for role in self.build.DESKTOP_BUILD_INPUTS:
            if role=='backend':continue
            tree=root/role;tree.mkdir()
            if role=='go':tool,files=self.go_role(tree)
            else:
                tool=tree/(role+'.exe');tool.write_bytes(b'inert tool fixture; never executable')
                files={tool.name:self.build.pin(tool)}
            descriptor=root/(role+'.json')
            descriptor.write_text(json.dumps(dict(schema=1,entry=tool.relative_to(tree).as_posix(),files=files)))
            self.policy['build_inputs'][role]=self.build.pin(descriptor)
        source=Path(self.build.__file__).parent/'desktop-host.json';source.write_text(json.dumps(self.policy))
        target=self.root/'staged'
        self.build.stage_desktop_tools(self.input,target)
        self.assertEqual(set(self.build.desktop_tool_inputs(target)),set(self.build.DESKTOP_BUILD_INPUTS)-{'backend'})
        go_descriptor=root/'go.json';original_go=go_descriptor.read_bytes()
        go_module=root/'go/module/go.mod';original_module=go_module.read_bytes()
        for variant in ('module','proxy'):
            value=json.loads(original_go)
            if variant=='module':
                go_module.write_bytes(original_module.replace(b'abstraction-provider-kit/go',b'abstraction-provider-kit',1))
                value['files']['module/go.mod']=self.build.pin(go_module)
            else:
                value['files'].pop('proxy/fixture');(root/'go/proxy/fixture').unlink()
            go_descriptor.write_text(json.dumps(value));self.policy['build_inputs']['go']=self.build.pin(go_descriptor)
            source.write_text(json.dumps(self.policy))
            rejected=self.root/('rejected-'+variant)
            with self.subTest(variant=variant),patch.object(self.build.subprocess,'run',side_effect=AssertionError('no child before admission')):
                with self.assertRaisesRegex(ValueError,'provider-kit|offline proxy'):
                    self.build.stage_desktop_tools(self.input,rejected)
            self.assertFalse(rejected.exists())
            go_module.write_bytes(original_module)
            if variant=='proxy':(root/'go/proxy/fixture').write_bytes(b'inert prepared proxy member')
        go_descriptor.write_bytes(original_go);self.policy['build_inputs']['go']=self.build.pin(go_descriptor)
        source.write_text(json.dumps(self.policy))
        original=(root/'node.json').read_bytes()
        (root/'node/node.exe').write_bytes(b'coordinated foreign tool')
        value=json.loads(original);value['files']['node.exe']=self.build.pin(root/'node/node.exe')
        (root/'node.json').write_text(json.dumps(value))
        rejected=self.root/'rejected'
        with self.assertRaisesRegex(ValueError,'descriptor differs'):
            self.build.stage_desktop_tools(self.input,rejected)
        self.assertFalse(rejected.exists())

    def test_desktop_prepare_uses_admitted_tools_and_emits_only_unsigned_inner_stage(self):
        tools=self.input/'desktop-inputs';tools.mkdir()
        for role in self.build.DESKTOP_BUILD_INPUTS:
            if role=='backend':continue
            tree=tools/role;tree.mkdir()
            if role=='go':entry,files=self.go_role(tree)
            else:
                entry=tree/('index.cjs' if role=='sdk' else role+'.exe');entry.write_bytes(b'inert source-pinned tool')
                files={entry.name:self.build.pin(entry)}
            if role=='pnpm':
                dependency=tree/'node_modules/pinned.js';dependency.parent.mkdir();dependency.write_bytes(b'inert locked dependency')
                files['node_modules/pinned.js']=self.build.pin(dependency)
            descriptor=tools/(role+'.json');descriptor.write_text(json.dumps(dict(schema=1,entry=entry.relative_to(tree).as_posix(),files=files)))
            self.policy['build_inputs'][role]=self.build.pin(descriptor)
        (self.build.HERE/'desktop-host.json').write_text(json.dumps(self.policy));self.file.write_text(json.dumps(self.policy))
        backend=self.input/'backend/desktop-backend.json';backend.parent.mkdir()
        descriptor=backend.parent/'backend-inputs.json';descriptor.write_bytes(b'{"schema":1}')
        self.policy['build_inputs']['backend']=self.build.pin(descriptor)
        (self.build.HERE/'desktop-host.json').write_text(json.dumps(self.policy))
        manifest=backend.parent/'backend-manifest.json';manifest.write_text(json.dumps(dict(id='win-amd',manager=False,
            input_descriptor_sha256=self.policy['build_inputs']['backend']['sha256'],
            comfyui_ref='b'*40,comfyui_commit='b'*40,python_version='3.12.10')))
        producer=backend.parent/'backend-producer.json';producer.write_bytes(b'{"schema":3}')
        recipe=json.loads(self.build.BACKEND_SOURCE.read_bytes())
        recipe['data_release']={'repository':'openabstractions/redist','tag_prefix':'comfyui-data-win-amd-'}
        tool_policy=recipe['archive_tool'];archive_tools=self.input/'archive-tool';archive_tools.mkdir()
        for record,name in [(tool_policy,tool_policy['name']),(tool_policy['source'],tool_policy['source']['name']),
                            *[(row,'notices/'+row['name']) for row in tool_policy['notices']]]:
            member=archive_tools/name;member.parent.mkdir(parents=True,exist_ok=True)
            member.write_bytes(self.pe() if name=='7zr.exe' else b'inert full source or notice DATA')
            record.update(self.build.pin(member))
        self.build.BACKEND_SOURCE.write_text(json.dumps(recipe))
        recipe_path=backend.parent/'backend-amd-v1.json';recipe_path.write_bytes(self.build.BACKEND_SOURCE.read_bytes())
        def pinned(path):return dict(name=path.name,**self.build.pin(path))
        def logical(name,format):
            value=dict(name=name,bytes=1,sha256=hashlib.sha256(b'x').hexdigest())
            return dict(value,format=format,parts=[value])
        spec=importlib.util.spec_from_file_location('fixture_data_policy',self.root/'scripts/app_pair_receipt.py')
        pair=importlib.util.module_from_spec(spec);spec.loader.exec_module(pair)
        resource=dict(schema=2,variant='win-amd',comfyui_ref='b'*40,python_version='3.12.10',
            archive=logical('backend.7z','7z'),corresponding_source=logical('backend-source.zip','zip'),
            data_release=pair.backend_data_release(logical('backend.7z','7z')),provenance=dict(input_descriptor=pinned(descriptor),
                manifest=pinned(manifest),producer_receipt=pinned(producer),backend_policy=pinned(recipe_path)))
        backend.write_text(json.dumps(resource))
        bridge=self.input/'desktop_backend.cjs';bridge.write_bytes(b'// inert source-selected bridge')
        icon=self.input/'oa-comfyui.ico';icon.write_bytes(b'inert source-owned icon')
        self.received.write_text(json.dumps(dict(build_inputs={path.relative_to(self.input).as_posix():self.build.pin(path)
            for path in self.input.rglob('*') if path.is_file()})))
        source=self.root/'upstream-build';source.mkdir();out=self.root/'desktop-native'
        args=argparse.Namespace(input_root=self.input,source_root=source,input_receipt=self.received,git='selected-git',version='0.8.0')
        def prepared(source,destination,git,*,git_environment_owner):
            self.assertEqual(git_environment_owner,self.git_owner)
            self.assertEqual(git,'selected-git');destination.mkdir();(destination/'desktop-source-receipt.json').write_bytes(b'{}')
        calls=[]
        def build(command,**kwargs):
            calls.append(command);self.assertEqual(kwargs['cwd'],source)
            self.assertEqual(kwargs['env']['npm_config_ignore_scripts'],'true')
            self.assertNotIn('CSC_LINK',kwargs['env'])
            if '--dir' in command:
                folder=out/'desktop-output/win-unpacked';folder.mkdir(parents=True)
                (folder/'OpenAbstractions ComfyUI.exe').write_bytes(self.pe())
                (folder/'resources').mkdir()
                shutil.copytree(source/'oa-backend',folder/'resources/oa-backend')
                shutil.copytree(source/'oa-sdk',folder/'resources/oa-sdk')
            return subprocess.CompletedProcess(command,0)
        supplier={'desktop/resources/oa-backend/archive-tool/7zr.exe':self.build.pin(archive_tools/'7zr.exe')}
        def helper(tools,shared,output,destination):
            target=destination/'launch-helper.exe';target.write_bytes(self.pe())
            (destination/'launch-helper-pin.json').write_bytes(self.build.desktop_helper_carrier(self.build.pin(target)))
        with patch.object(self.build,'prepare_desktop_source',side_effect=prepared),patch.object(self.build.subprocess,'run',side_effect=build), \
             patch.object(self.build,'desktop_supplier_members',return_value=supplier), \
             patch.object(self.build,'build_backend_launcher',side_effect=helper) as compile_helper:
            self.assertEqual(self.build.desktop_prepare(args,self.shared,out),0)
            compile_helper.assert_called_once()
        self.assertEqual(len(calls),2);self.assertIn('run',calls[0]);self.assertIn('--dir',calls[1])
        self.assertNotIn('install',[part for call in calls for part in call])
        receipt=json.loads((out/'desktop-native-receipt.json').read_bytes())
        self.assertEqual(receipt['schema'],3);self.assertEqual(receipt['stage'],'desktop-inner-unsigned')
        self.assertEqual(set(receipt['native_files']),{'desktop/OpenAbstractions ComfyUI.exe',self.build.BACKEND_HELPER,*supplier})
        self.assertEqual(receipt['supplier_preserved'],supplier)
        self.assertEqual(json.loads((out/'desktop/resources/oa-sdk/sdk-pin.json').read_bytes()),dict(name='sdk.json',**self.policy['build_inputs']['sdk']))
        self.assertEqual((out/'desktop/resources/oa-backend/desktop-backend.json').read_bytes(),backend.read_bytes())
        self.assertNotIn('installer',receipt);self.assertNotIn('source_pair',receipt)
        backend.write_bytes(b'coordinated replacement backend')
        with patch.object(self.build.subprocess,'run',side_effect=AssertionError('no child on drift')):
            with self.assertRaisesRegex(ValueError,'unmeasured Desktop build input'):
                self.build.desktop_prepare(args,self.shared,self.root/'must-not-build')
        self.assertFalse((self.root/'must-not-build').exists())

    def test_native_signing_partition_preserves_only_source_selected_official_archiver(self):
        supplier=self.build.desktop_supplier_members()
        owned={'desktop/Owned.exe':dict(bytes=128,sha256='e'*64)}
        value=dict(schema=3,native_files={**owned,**supplier},supplier_preserved=supplier)
        self.assertEqual(self.build.desktop_signing_members(value),owned)
        variants=[]
        for field in ('native_files','supplier_preserved'):
            changed=json.loads(json.dumps(value));name=next(iter(supplier));changed[field][name]['sha256']='0'*64;variants.append(changed)
        changed=json.loads(json.dumps(value));changed['supplier_preserved'].update(owned);variants.append(changed)
        changed=json.loads(json.dumps(value));changed['schema']=2;variants.append(changed)
        for changed in variants:
            with self.subTest(value=changed),self.assertRaises(ValueError):self.build.desktop_signing_members(changed)
        self.assertEqual(self.build.desktop_signing_members(dict(schema=2,native_files=owned)),owned)

    def test_inner_desktop_native_inventory_is_closed_and_rejects_hidden_pe(self):
        desktop=self.root/'built-desktop';desktop.mkdir()
        pe=bytearray(128);pe[:2]=b'MZ';pe[60:64]=(64).to_bytes(4,'little');pe[64:68]=b'PE\0\0'
        (desktop/'OpenAbstractions ComfyUI.exe').write_bytes(pe)
        (desktop/'data.txt').write_bytes(b'plain')
        rows=self.build.desktop_native_inventory(desktop)
        self.assertEqual([row['name'] for row in rows],['OpenAbstractions ComfyUI.exe'])
        (desktop/'hidden.dat').write_bytes(pe)
        with self.assertRaisesRegex(ValueError,'undeclared'):
            self.build.desktop_native_inventory(desktop)
        (desktop/'hidden.dat').unlink();(desktop/'OpenAbstractions ComfyUI.exe').write_bytes(b'MZ')
        with self.assertRaisesRegex(ValueError,'malformed'):
            self.build.desktop_native_inventory(desktop)
    def binding(self):
        return self.build.binding(self.file,self.input,authenticated_receipt_pin=self.build.pin(self.receipt),signing_policy=self.root/'certum_simplysign.json',expected_source='a'*40,expected_run=101,expected_attempt=2)
    def receive(self,command,**kwargs):
        self.assertEqual(Path(kwargs['cwd']),self.out)
        if command[0]=='fixture-rc':Path(command[command.index('/fo')+1]).write_bytes(b'inert resource')
        else:
            self.assertEqual(command[0],'fixture-cl')
            self.assertIn('/FI'+str(self.out/'desktop_inputs.h'),command)
            Path(next(value[3:] for value in command if value.startswith('/Fe'))).write_bytes(b'inert native output; never executable')
        return subprocess.CompletedProcess(command,0)
    def test_complete_binding_and_native_receipt_preserve_selected_inputs(self):
        with patch.object(self.build.subprocess,'run',side_effect=self.receive) as process:self.assertEqual(self.build.main(self.argv),0)
        self.assertEqual(process.call_count,4)
        receipt=json.loads((self.out/'preparation-native-receipt.json').read_bytes())
        self.assertEqual(receipt['desktop_binding'],self.build.pin(self.file))
        self.assertEqual(set(receipt['outputs']),{'comfyui-prepare-instance.exe','comfyui-hostfolder.dll','desktop-uninstaller.exe'})
        self.assertIn('generated:desktop_inputs.h',receipt['inputs'])
        with patch.object(self.build.subprocess,'run',side_effect=AssertionError('no process on stale output')):
            with self.assertRaisesRegex(ValueError,'fresh output'):self.build.main(self.argv)
    def test_source_policy_and_unadmitted_inputs_refuse_before_process(self):
        self.policy['build_inputs']={}
        (self.build.HERE/'desktop-host.json').write_text(json.dumps(self.policy));self.file.write_text(json.dumps(self.policy))
        with patch.object(self.build.subprocess,'run',side_effect=AssertionError('pre-effect admission')):
            with self.assertRaisesRegex(ValueError,'require admission'):self.build.main(self.argv)

    def test_helper_receiving_rejects_wrong_stage_and_original_signer_identity(self):
        original=self.signed.read_bytes()
        for field,value in [('stage','desktop-final'),('source','b'*40),('run_id',102),('run_attempt',3)]:
            changed=json.loads(original);changed[field]=value;self.signed.write_text(json.dumps(changed))
            argv=self.argv[:];measured=self.build.pin(self.signed)
            argv[argv.index('--signed-desktop-sha256')+1]=measured['sha256']
            argv[argv.index('--signed-desktop-bytes')+1]=str(measured['bytes'])
            with self.subTest(field=field),patch.object(self.build.subprocess,'run',side_effect=AssertionError('no child on wrong stage')):
                with self.assertRaisesRegex(ValueError,'authenticated stage/source/run/attempt'):self.build.main(argv)
        self.signed.write_bytes(original)

    def test_unsigned_package_intent_and_signed_transformation_have_separate_pins(self):
        root=self.input;folder=root/'package';folder.mkdir()
        installer=self.declared['installer'];shutil.copyfile(root/installer['name'],folder/installer['name'])
        value={key:row for key,row in self.declared.items() if key!='source_pair'}
        value.update(stage='desktop-packaged-unsigned',run_attempt=3)
        receipt=folder/'desktop-package-receipt.json';receipt.write_text(json.dumps(value));measured=self.build.pin(receipt)
        arguments=dict(expected_source='a'*40,expected_run=101,expected_attempt=3,signing_policy=self.root/'certum_simplysign.json')
        self.assertEqual(self.build.custom_desktop_package(root,measured,**arguments),value)
        with self.assertRaisesRegex(ValueError,'stage/source/run/attempt'):
            self.build.custom_desktop_package(root,measured,**dict(arguments,expected_attempt=2))
        target=folder/installer['name'];target.write_bytes(b'actual-shaped transformed own bytes')
        certificate=json.loads((self.root/'certum_simplysign.json').read_bytes())['certificate']
        proof=dict(name=installer['name'],**self.build.pin(target),certificate={key:certificate[key] for key in ('sha1','sha256','subject','issuer')},
            file_digest='SHA256',timestamp_digest='SHA256',timestamp='RFC3161')
        self.assertEqual(self.build.custom_desktop_package(root,measured,signed_installer=proof,**arguments)['installer'],installer)
        with self.assertRaisesRegex(ValueError,'member differs'):
            self.build.custom_desktop_package(root,measured,**arguments)
        wrong=dict(proof,certificate=dict(proof['certificate'],subject='Supplier'))
        with self.assertRaisesRegex(ValueError,'OA signature observation'):
            self.build.custom_desktop_package(root,measured,signed_installer=wrong,**arguments)
        value['installer'].update(self.build.pin(target));receipt.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'member differs'):
            self.build.custom_desktop_package(root,measured,signed_installer=proof,**arguments)

    def test_uninstaller_recreation_is_checked_before_helpers(self):
        first=self.root/'first.exe';second=self.root/'second.exe'
        first.write_bytes(b'same unsigned stub');second.write_bytes(first.read_bytes())
        with patch.object(self.build,'nsis_package',side_effect=[first,second]) as nsis:
            self.real_prepare_uninstaller(None,self.out)
        self.assertEqual(nsis.call_count,2)
        self.assertEqual((self.out/'desktop-uninstaller.exe').read_bytes(),first.read_bytes())
        second.write_bytes(b'different recreation')
        with patch.object(self.build,'nsis_package',side_effect=[first,second]):
            with self.assertRaisesRegex(ValueError,'nondeterministic'):self.real_prepare_uninstaller(None,self.out)
        self.assertEqual((self.out/'desktop-uninstaller.exe').read_bytes(),first.read_bytes())

    def test_native_fixture_cleanup_refuses_external_or_linked_targets(self):
        allowed=self.root/'cleanup-owned';allowed.mkdir()
        owned=allowed/'plain';owned.mkdir();(owned/'file').write_bytes(b'own disposable data')
        cleanup_fixture(allowed,owned);self.assertFalse(owned.exists())
        foreign=self.root/'foreign-sentinel';foreign.mkdir();sentinel=foreign/'keep';sentinel.write_bytes(b'keep')
        for target in (foreign,allowed):
            with self.assertRaises(ValueError):cleanup_fixture(allowed,target)
        if os.name=='nt':
            import _winapi
            junction=allowed/'redirect';_winapi.CreateJunction(str(foreign),str(junction))
            try:
                with self.assertRaises(ValueError):cleanup_fixture(allowed,junction)
                self.assertEqual(sentinel.read_bytes(),b'keep')
            finally:os.rmdir(junction)
        self.assertEqual(sentinel.read_bytes(),b'keep')
    def test_coordinated_payload_receipt_mutation_cannot_replace_authenticated_pin(self):
        frozen=json.loads(self.received.read_bytes())['build_inputs']['desktop-build-receipt.json']
        exe=self.input/self.declared['host']['exe']['name'];exe.write_bytes(b'coordinated changed own payload')
        self.declared['host']['exe'].update(self.build.pin(exe));self.declared['native_files'][self.declared['host']['exe']['name']].update(self.build.pin(exe))
        self.receipt.write_text(json.dumps(self.declared))
        with self.assertRaisesRegex(ValueError,'member differs'):
            self.build.custom_desktop_binding(self.input,frozen,self.root/'certum_simplysign.json',expected_source='a'*40,expected_run=101,expected_attempt=2)
    def test_policy_identity_and_native_closure_cannot_be_redefined_by_receipt(self):
        original=json.loads(json.dumps(self.declared))
        for field in ('source_policy','build_inputs','identity'):
            self.declared=json.loads(json.dumps(original));self.declared[field]={};self.save()
            with self.assertRaisesRegex(ValueError,'authenticated stage/source/run/attempt'):self.binding()
        self.declared=json.loads(json.dumps(original));self.declared['host']['publisher']['subject']='CN=Drip Artificial Inc';self.save()
        with self.assertRaisesRegex(ValueError,'host identity'):self.binding()
        self.declared=json.loads(json.dumps(original));self.save();(self.input/'desktop/extra.dll').write_bytes(b'unlisted native file')
        with self.assertRaisesRegex(ValueError,'malformed|closure'):self.binding()
    def test_historical_supplier_binding_has_no_automatic_custom_fallback(self):
        self.file.write_text(json.dumps(dict(schema=1,official_installer={},host={})))
        with self.assertRaisesRegex(ValueError,'historical supplier schema1'):self.binding()
    def test_binding_cannot_be_selected_outside_input_root(self):
        with self.assertRaises(FileNotFoundError):self.build.binding(self.file,self.root/'missing')
        outside=self.root/'other';outside.mkdir()
        with self.assertRaisesRegex(ValueError,'outside admitted root'):self.build.binding(self.file,outside)
    def test_unmeasured_shared_header_shadow_refuses_before_processes(self):
        for parent in (self.build.HERE,self.out):
            parent.mkdir(exist_ok=True)
            for name in ('user_token.h','hostfolder_io.h','native_windows.h'):
                shadow=parent/name;shadow.write_bytes(b'unmeasured shadow')
                with patch.object(self.build.subprocess,'run',side_effect=AssertionError('no process on shadow')):
                    with self.assertRaisesRegex(ValueError,'conflicting selected shared'):self.build.main(self.argv)
                shadow.unlink()
    def test_binding_and_generated_header_drift_refuse_receipt(self):
        for target in ('binding','generated'):
            with self.subTest(target=target):
                self.file.write_text(json.dumps(self.policy))
                self.out=self.root/target;self.out.mkdir();shutil.copytree(self.input/'desktop',self.out/'desktop')
                shutil.copyfile(self.signed,self.out/self.signed.name);shutil.copyfile(self.input/'desktop-signatures.json',self.out/'desktop-signatures.json')
                argv=self.argv[:];argv[argv.index('--out')+1]=str(self.out);argv[argv.index('--signed-desktop-receipt')+1]=str(self.out/self.signed.name)
                def changed(command,**kwargs):
                    result=self.receive(command,**kwargs)
                    if command[0]=='fixture-cl':
                        path=self.file if target=='binding' else self.out/'desktop_inputs.h'
                        path.write_bytes(path.read_bytes()+b' ')
                    return result
                with patch.object(self.build.subprocess,'run',side_effect=changed):
                    with self.assertRaisesRegex(ValueError,'input changed'):self.build.main(argv)
                self.assertFalse((self.out/'preparation-native-receipt.json').exists())
    def test_source_roles_derive_existing_action_identity(self):
        own,supplier,external=self.build.source_roles(self.shared)
        self.assertEqual(own,(('Binary','HostFolderAction','comfyui-hostfolder.dll'),))
        self.assertEqual(supplier,());self.assertEqual(external,('comfyui-prepare-instance.exe',))
        fragment=self.shared/'extension.wxs';fragment.write_text(fragment.read_text().replace('DllEntry="FindHostFolder"','DllEntry="Unexpected"'))
        with self.assertRaisesRegex(ValueError,'one host-folder'):self.build.source_roles(self.shared)

    def test_source_preparation_receives_exact_checkout_patch_and_output(self):
        source=self.root/'desktop-checkout';(source/'src').mkdir(parents=True)
        before=b'export const fixture = 1\n';after=b'export const fixture = 2\n'
        target=source/'src/fixture.ts';target.write_bytes(before)
        policy,_=self.build.desktop_source_policy()
        license_file=source/'LICENSE';license_file.write_bytes(b'inert AGPL source fixture')
        policy=dict(policy,licensing=dict(expression='AGPL-3.0-or-later',file='LICENSE',**self.build.pin(license_file)))
        policy=dict(policy,inputs={'src/fixture.ts':dict(sha256=hashlib.sha256(before).hexdigest(),
            normalized_sha256=hashlib.sha256(before).hexdigest(),patched_sha256=hashlib.sha256(after).hexdigest())})
        lock=source/'pnpm-lock.yaml';lock.write_bytes(b'inert locked fixture')
        policy=dict(policy,lock=dict(file=lock.name,**self.build.pin(lock)))
        selected=self.root/'fixture.patch';selected.write_bytes(b'inert selected patch command receiver')
        commands=[]
        prefix=['selected-git','-C',str(source)]
        def check_environment(command,kwargs):
            self.assertEqual(command[:len(prefix)],prefix)
            environment=kwargs['env']
            self.assertFalse(any(key.upper() in ('GIT_DIR','GIT_WORK_TREE','GIT_INDEX_FILE') for key in environment))
            self.assertEqual(environment['GIT_CONFIG_GLOBAL'],os.devnull)
            self.assertEqual(environment['GIT_CONFIG_SYSTEM'],os.devnull)
            self.assertEqual(environment['GIT_CONFIG_NOSYSTEM'],'1')
            self.assertEqual(environment['GIT_CONFIG_COUNT'],'2')
            self.assertEqual(environment['GIT_CONFIG_KEY_0'],'core.fsmonitor')
            self.assertEqual(environment['GIT_CONFIG_VALUE_0'],'false')
            self.assertEqual(environment['GIT_CONFIG_KEY_1'],'core.hooksPath')
            self.assertEqual(environment['GIT_CONFIG_VALUE_1'],os.devnull)
        def read(command,**kwargs):
            check_environment(command,kwargs)
            return policy['upstream']['commit'] if command[len(prefix):]==['rev-parse','HEAD'] else ''
        def apply(command,**kwargs):
            check_environment(command,kwargs)
            commands.append(command)
            self.assertTrue(kwargs['check'])
            if command==[*prefix,'apply',str(selected)]:target.write_bytes(after)
        out=self.root/'source-receipt'
        with patch.object(self.build,'desktop_source_policy',return_value=(policy,selected)), \
             patch.object(self.build.subprocess,'check_output',side_effect=read),patch.object(self.build.subprocess,'run',side_effect=apply), \
             patch.dict(os.environ,{'GIT_DIR':'foreign','GIT_WORK_TREE':'foreign','GIT_INDEX_FILE':'foreign','GIT_CONFIG_COUNT':'1',
                                   'GIT_CONFIG_KEY_0':'core.fsmonitor','GIT_CONFIG_VALUE_0':'foreign-command'}):
            self.assertEqual(self.build.prepare_desktop_source(source,out,'selected-git',git_environment_owner=self.git_owner),0)
        self.assertEqual(commands,[[*prefix,'apply','--check',str(selected)],[*prefix,'apply',str(selected)]])
        received=json.loads((out/'desktop-source-receipt.json').read_bytes())
        self.assertEqual(received['inputs'],policy['inputs'])
        self.assertEqual(received['patch'],self.build.pin(selected))
        self.assertIn('no binary',received['authority'])
        license_file.write_bytes(b'foreign license text')
        with patch.object(self.build,'desktop_source_policy',return_value=(policy,selected)), \
             patch.object(self.build.subprocess,'check_output',side_effect=AssertionError('no Git for changed license')):
            with self.assertRaisesRegex(ValueError,'license differs'):
                self.build.prepare_desktop_source(source,self.root/'changed-license',git_environment_owner=self.git_owner)
        self.assertFalse((self.root/'changed-license').exists())
    def test_changed_source_stops_before_git_or_output(self):
        source=self.root/'wrong-checkout';source.mkdir()
        policy,_=self.build.desktop_source_policy()
        license_file=source/'LICENSE';license_file.write_bytes(b'inert AGPL source fixture')
        policy=dict(policy,licensing=dict(expression='AGPL-3.0-or-later',file='LICENSE',**self.build.pin(license_file)))
        policy=dict(policy,inputs={'source.ts':dict(sha256='1'*64,normalized_sha256='1'*64,patched_sha256='2'*64)})
        lock=source/'pnpm-lock.yaml';lock.write_bytes(b'inert locked fixture')
        policy=dict(policy,lock=dict(file=lock.name,**self.build.pin(lock)))
        (source/'source.ts').write_bytes(b'wrong source')
        with patch.object(self.build,'desktop_source_policy',return_value=(policy,self.root/'unused.patch')), \
             patch.object(self.build.subprocess,'check_output',side_effect=AssertionError('no Git before source pins')):
            with self.assertRaisesRegex(ValueError,'before patch'):self.build.prepare_desktop_source(source,self.root/'no-output',git_environment_owner=self.git_owner)
        self.assertFalse((self.root/'no-output').exists())

    def test_package_receives_final_native_pins_and_rechecks_descriptor(self):
        # Substitute the existing package owner; compiler/WiX/installer never run.
        stub='''import json,hashlib
from pathlib import Path
def read_extension(path):return json.loads(path.read_text())
def native_identity_source():return Path(__file__).resolve().parents[2]/'openabstractions-flat/abstraction-identity/fileid/native_windows.h'
def main(argv):
 out=Path(argv[argv.index('--out')+1]);out.mkdir(exist_ok=True)
 msi=out/'comfyui-0.8.0.msi';msi.write_bytes(b'inert MSI fixture')
 (out/'bundle-app.json').write_text(json.dumps({'id':'comfyui','file':msi.name,'sha256':hashlib.sha256(msi.read_bytes()).hexdigest(),'bytes':msi.stat().st_size}))
 (out/'installer-receipt.json').write_text('{}')
 if read_extension(Path(argv[argv.index('--extension')+1])).get('drift_receipt'):
  receipt=out.parent/'preparation-native-receipt.json';receipt.write_bytes(receipt.read_bytes()+b' ')
 return 0
'''
        (self.shared/'build_extension.py').write_text(stub)
        with patch.object(self.build.subprocess,'run',side_effect=self.receive):self.build.main(self.argv)
        descriptor=self.root/'extension.json';descriptor.write_text('{"id":"comfyui"}')
        argv=self.argv+['--phase','package','--extension',str(descriptor),'--include-root',str(self.input),'--package-source','a'*40,'--package-run','101','--package-attempt','3']
        for role,name in [('action','comfyui-hostfolder.dll'),('preparation','comfyui-prepare-instance.exe'),('uninstaller','desktop-uninstaller.exe')]:
            path=self.out/name;value=self.build.pin(path)
            argv+=['--'+role,str(path),'--'+role+'-sha256',value['sha256'],'--'+role+'-bytes',str(value['bytes'])]
        host=self.root/'OpenAbstractions-ComfyUI-1.1.8-win-x64.exe';host.write_bytes(b'inert unsigned NSIS')
        patch.object(self.build,'nsis_package',return_value=host).start()
        with patch.object(self.build.subprocess,'run',side_effect=AssertionError('no physical package effect')):
            self.assertEqual(self.build.main(argv),0)
        app=json.loads((self.out/'package/bundle-app.json').read_bytes())
        self.assertNotIn('download_url',app['host'])
        self.assertEqual(Path(app['host']['file']),self.out/'package'/self.declared['installer']['name'])
        self.assertTrue(Path(app['host']['preparation']['file']).is_absolute())
        self.assertEqual(self.build.pin(Path(app['file'])),{key:app[key] for key in ('sha256','bytes')})
        self.assertEqual(json.loads((self.out/'package/installer-receipt.json').read_bytes())['fixture_removal'],
                         {'profile':'nsis-current-user','basename':'Uninstall OpenAbstractions ComfyUI.exe'})
        descriptor.write_text('{"id":"another"}')
        with self.assertRaisesRegex(ValueError,'must select ComfyUI'):self.build.main(argv)
        descriptor.write_text('{"id":"comfyui","drift_receipt":true}')
        prep=self.out/'preparation-native-receipt.json';original=prep.read_bytes()
        with self.assertRaisesRegex(ValueError,'received input changed'):self.build.main(argv)
        prep.write_bytes(original);descriptor.write_text('{"id":"comfyui"}')
        (self.out/'comfyui-hostfolder.dll').write_bytes(b'changed after signing')
        with self.assertRaisesRegex(ValueError,'changed final signed'):self.build.main(argv)

    def test_modern_package_uses_authenticated_embedded_wrapper_without_extension_msi(self):
        stub='''import json
from pathlib import Path
def read_extension(path):return json.loads(path.read_text())
def native_identity_source():return Path(__file__).resolve().parents[2]/'openabstractions-flat/abstraction-identity/fileid/native_windows.h'
def main(argv):raise AssertionError('redundant extension MSI must not run')
'''
        (self.shared/'build_extension.py').write_text(stub)
        with patch.object(self.build.subprocess,'run',side_effect=self.receive):self.build.main(self.argv)
        descriptor=self.root/'extension.json';descriptor.write_text(json.dumps(dict(id='comfyui',title='OpenAbstractions for ComfyUI')))
        argv=self.argv+['--phase','package','--extension',str(descriptor),'--include-root',str(self.input),
            '--package-source','a'*40,'--package-run','101','--package-attempt','3']
        for role,name in [('action','comfyui-hostfolder.dll'),('preparation','comfyui-prepare-instance.exe'),('uninstaller','desktop-uninstaller.exe')]:
            path=self.out/name;value=self.build.pin(path)
            argv+=['--'+role,str(path),'--'+role+'-sha256',value['sha256'],'--'+role+'-bytes',str(value['bytes'])]
        captured=[]
        with patch.object(self.build,'package',side_effect=lambda *args:captured.append(args) or 0):self.build.main(argv)
        args,shared,out,declared=captured.pop()
        declared['custom_build']['schema']=3;declared['custom_build']['supplier_preserved']={}
        base=self.out/'desktop/resources/oa-backend';base.mkdir()
        prefix='ComfyUI/custom_nodes/abstraction_downloads/'
        names=('__init__.py','oa_connect.py','oa_relay.py','oa_helper.py','oa_interpreter.py',
            'oa_atomic/abstraction_cas.py','oa_nodes/__init__.py','oa_client/abstraction/ipc/__init__.py',
            'application-manifest.json','helper/python.exe','helper/python312.dll')
        def pin(raw):return dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        files={prefix+name:pin(('inert '+name).encode()) for name in names}
        template=b'home = {backend}/standalone-env\ninclude-system-site-packages = false\n'
        files[prefix+'helper/pyvenv.cfg']=pin(template);files['standalone-env/python.exe']=files[prefix+'helper/python.exe']
        integration=dict(name='integration.zip',**pin(b'inert integration'))
        manifest=dict(manager=False,python_version='3.12.10',oa_integration=integration,runtime_files=files,
            helper_python_config=dict(name=prefix+'helper/pyvenv.cfg',template=pin(template)))
        raw=json.dumps(manifest).encode();(base/'backend-manifest.json').write_bytes(raw)
        archive=dict(name='backend.7z',**pin(b'opaque fixture'),format='7z',parts=[dict(name='backend.7z',**pin(b'opaque fixture'))])
        source=dict(name='backend-source.zip',**pin(b'opaque source'),format='zip',parts=[dict(name='backend-source.zip',**pin(b'opaque source'))])
        resource=dict(schema=2,variant='win-amd',comfyui_ref='f'*40,python_version='3.12.10',archive=archive,corresponding_source=source,
            data_release=dict(repository='openabstractions/fixture-data',tag='fixture-1'),provenance=dict(
                input_descriptor=dict(name='backend-inputs.json',**self.policy['build_inputs']['backend']),
                manifest=dict(name='backend-manifest.json',**pin(raw)),
                backend_policy=dict(name='backend-amd-v1.json',**pin(b'policy')),
                producer_receipt=dict(name='backend-producer.json',**pin(b'producer'))))
        (base/'desktop-backend.json').write_text(json.dumps(resource))
        host=self.root/'OpenAbstractions-ComfyUI-1.1.8-win-x64.exe';host.write_bytes(b'inert unsigned NSIS')
        with patch.object(self.build,'nsis_package',return_value=host),patch.object(self.build.subprocess,'run',side_effect=AssertionError('physical effect blocked')):
            self.assertEqual(self.build.package(args,shared,out,declared),0)
        row=json.loads((out/'package/bundle-app.json').read_bytes())
        self.assertEqual(set(row),{'id','title','host','embedded_integration'})
        self.assertEqual(row['embedded_integration']['backend_manifest'],resource['provenance']['manifest'])
        received=json.loads((out/'package/installer-receipt.json').read_bytes())
        self.assertEqual(received['schema'],3);self.assertNotIn('expected_payload',received)
        self.assertNotIn('msi',received);self.assertFalse(list((out/'package').glob('*.msi')))
        (base/'backend-manifest.json').write_bytes(raw+b' ')
        with self.assertRaisesRegex(ValueError,'manifest differs'):
            self.build.desktop_embedded_integration(out,declared,shared)


def cleanup_fixture(root,target):
    """Retain a failed fixture with links instead of traversing foreign state."""
    root=Path(root).resolve();target=Path(target).absolute()
    if target==root or not target.is_relative_to(root) or target.resolve()!=target or target.is_symlink() or target.is_junction():
        raise ValueError('fixture cleanup target is not an owned plain child')
    pending=[target];count=0
    while pending:
        for member in pending.pop().iterdir():
            count+=1
            if count>10000 or member.is_symlink() or member.is_junction() or getattr(member.lstat(),'st_file_attributes',0)&0x400:
                raise ValueError('fixture retained: linked/over-bound cleanup member')
            if member.is_dir():pending.append(member)
    shutil.rmtree(target)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture-exe',type=Path,required=True)
    parser.add_argument('--fixture-root',type=Path,required=True)
    parser.add_argument('--preparation-fixtures',action='store_true',help='Include inert host bytes for Win32 API-substituted preparation receiving tests')
    args=parser.parse_args()
    root=args.fixture_root.resolve()
    if '.forks' not in root.parts:
        parser.error('fixture root must be an ignored worktree .forks directory')
    executable=args.fixture_exe.resolve()
    if executable.name!='test.exe' or not executable.is_relative_to(root):
        parser.error('only the task-owned reader fixture executable is permitted')
    root.mkdir(parents=True,exist_ok=True)
    temporary=tempfile.mkdtemp(prefix='comfy-reader-',dir=root)
    junction=None
    try:
        base=Path(temporary)/'named \u03a9'
        host=base/'backend/ComfyUI'
        host.mkdir(parents=True)
        if args.preparation_fixtures and os.name=='nt':
            # Enable the owned empty directory before populating the receiver's
            # case-sensitive custom_nodes/CUSTOM_NODES identity falsifier.
            import ctypes
            from ctypes import wintypes
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
            kernel.CreateFileW.restype=wintypes.HANDLE
            kernel.SetFileInformationByHandle.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
            kernel.CloseHandle.argtypes=[wintypes.HANDLE]
            handle=kernel.CreateFileW(str(host),0x100,3,None,3,0x02000000,None)
            if handle==wintypes.HANDLE(-1).value:raise ctypes.WinError(ctypes.get_last_error())
            try:
                flags=wintypes.DWORD(1)
                if not kernel.SetFileInformationByHandle(handle,23,ctypes.byref(flags),4):raise ctypes.WinError(ctypes.get_last_error())
            finally:kernel.CloseHandle(handle)
        (host/'custom_nodes').mkdir()
        (host/'main.py').write_bytes(b'inert fixture; never executed')
        (base/'backend/standalone-env').mkdir()
        (base/'backend/standalone-env/python.exe').write_bytes(b'inert fixture; never executed')
        # Separate stock-host receiving fixtures preserve their literal layout.
        stock=base/'ComfyUI';(stock/'custom_nodes').mkdir(parents=True)
        (stock/'.venv/Scripts').mkdir(parents=True)
        (stock/'main.py').write_bytes(b'inert fixture; never executed')
        (stock/'.venv/Scripts/python.exe').write_bytes(b'inert fixture; never executed')
        (base/'Comfy Desktop').mkdir()
        (base/'Comfy Desktop/installations.json').write_text(json.dumps([dict(id='one',name='fixture',sourceId='standalone',status='installed',installPath=str(base))]),encoding='utf-8')
        # A development record must never be selected by the production reader.
        (base/'comfyui-desktop-2').mkdir()
        (base/'comfyui-desktop-2/installations.json').write_text('not authoritative',encoding='utf-8')
        if args.preparation_fixtures:
            (base/'Desktop').mkdir()
            (base/'Desktop/Comfy Desktop.exe').write_bytes(b'fixture-host')
            if os.name=='nt':
                import _winapi
                outside=base/'external-removal-sentinel';outside.mkdir()
                junction=base/'fixture-reparse-junction'
                _winapi.CreateJunction(str(outside),str(junction))
        return subprocess.run([str(args.fixture_exe.resolve()),str(base)],timeout=20,check=False).returncode
    finally:
        if junction is not None and junction.is_junction():
            # Remove only this explicitly created link, never recurse through it.
            os.rmdir(junction)
        elif junction is not None and junction.exists():
            raise ValueError('fixture retained: owned junction was replaced')
        cleanup_fixture(root,temporary)

if __name__=='__main__':raise SystemExit(main())

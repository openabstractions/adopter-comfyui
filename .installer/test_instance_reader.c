#include <assert.h>
#include <stdio.h>
#include "instance_reader.h"

#ifdef OA_COMFY_PREPARATION_TESTS
#include <msiquery.h>
#include <shlobj.h>
#include <shlwapi.h>
#include <wintrust.h>
#include <strsafe.h>
#include <user_token.h>
static const WCHAR *test_root;
static WCHAR test_found[4],test_taken[4],test_folder[32768];
static WCHAR test_removed[4],test_directory_identity[49],test_extension_identity[49];
static WCHAR test_rollback[32771],test_cad[32771];
static BOOL test_fail_extension_write;
static BOOL test_foreign_value,test_foreign_subkey;
static unsigned test_inject_foreign_at_delete,test_empty_deletes;
static BOOL test_record_exists,test_path_only;
static unsigned test_launches,test_dialogs;
static BOOL test_deny_primary;
static unsigned test_denied_primary_queries;
static BOOL WINAPI fixture_process_token(HANDLE process,DWORD access,PHANDLE token)
{if(test_deny_primary){test_denied_primary_queries++;SetLastError(ERROR_ACCESS_DENIED);return FALSE;}return OpenProcessToken(process,access,token);}
static HRESULT WINAPI fixture_known_folder(REFKNOWNFOLDERID folder,DWORD flags,HANDLE token,PWSTR *out)
{(void)folder;(void)flags;(void)token;size_t bytes=(wcslen(test_root)+1)*sizeof(WCHAR);*out=CoTaskMemAlloc(bytes);if(!*out)return E_OUTOFMEMORY;CopyMemory(*out,test_root,bytes);return S_OK;}
static LSTATUS WINAPI fixture_open_user(REGSAM access,PHKEY key){(void)access;*key=(HKEY)(ULONG_PTR)123;return ERROR_SUCCESS;}
static LSTATUS WINAPI fixture_close_key(HKEY key){(void)key;return ERROR_SUCCESS;}
static LSTATUS WINAPI fixture_registry(HKEY key,LPCWSTR sub,LPCWSTR value,DWORD flags,LPDWORD type,PVOID data,LPDWORD size)
{
    (void)key;(void)flags;(void)type;
    if(!sub){
        const WCHAR *saved=NULL;
        if(!wcscmp(value,L"directory_identity")&&*test_directory_identity)saved=test_directory_identity;
        else if(!wcscmp(value,L"extension_identity")&&*test_extension_identity)saved=test_extension_identity;
        else if(!wcscmp(value,L"folder")&&test_path_only)saved=test_folder;
        if(!saved)return ERROR_FILE_NOT_FOUND;
        DWORD bytes=(DWORD)((wcslen(saved)+1)*sizeof(WCHAR));
        if(!data){*size=bytes;return ERROR_SUCCESS;}
        if(*size<bytes)return ERROR_MORE_DATA;CopyMemory(data,saved,bytes);*size=bytes;return ERROR_SUCCESS;
    }
    if(wcsstr(sub,L"Extensions"))return ERROR_FILE_NOT_FOUND;
    WCHAR *text=calloc(32768,sizeof(WCHAR));if(!text)return ERROR_NOT_ENOUGH_MEMORY;
    if(!wcscmp(value,L"DisplayVersion"))wcscpy_s(text,32768,L"1.1.8");
    else if(!wcscmp(value,L"DisplayIcon"))swprintf_s(text,32768,L"%s\\Desktop\\Comfy Desktop.exe,0",test_root);
    else {free(text);return ERROR_FILE_NOT_FOUND;}
    DWORD bytes=(DWORD)((wcslen(text)+1)*sizeof(WCHAR));if(*size<bytes){free(text);return ERROR_MORE_DATA;}CopyMemory(data,text,bytes);free(text);*size=bytes;return ERROR_SUCCESS;
}
static LSTATUS WINAPI fixture_create_key(HKEY key,LPCWSTR sub,DWORD reserved,LPWSTR cls,DWORD options,REGSAM access,
                                         const LPSECURITY_ATTRIBUTES attributes,PHKEY result,LPDWORD disposition)
{(void)key;(void)reserved;(void)cls;(void)options;(void)access;(void)attributes;assert(!wcscmp(sub,L"Software\\OpenAbstractions\\Extensions\\comfyui"));*disposition=test_record_exists?REG_OPENED_EXISTING_KEY:REG_CREATED_NEW_KEY;test_record_exists=TRUE;*result=(HKEY)(ULONG_PTR)124;return ERROR_SUCCESS;}
static LSTATUS WINAPI fixture_open_key(HKEY key,LPCWSTR sub,DWORD options,REGSAM access,PHKEY result)
{(void)key;(void)options;(void)access;assert(!wcscmp(sub,L"Software\\OpenAbstractions\\Extensions\\comfyui"));if(!test_record_exists)return ERROR_FILE_NOT_FOUND;*result=(HKEY)(ULONG_PTR)124;return ERROR_SUCCESS;}
static LSTATUS WINAPI fixture_set_value(HKEY key,LPCWSTR name,DWORD reserved,DWORD kind,const BYTE *bytes,DWORD size)
{(void)key;(void)reserved;assert(kind==REG_SZ&&size==49*sizeof(WCHAR));WCHAR *target=!wcscmp(name,L"directory_identity")?test_directory_identity:!wcscmp(name,L"extension_identity")?test_extension_identity:NULL;assert(target);if(target==test_extension_identity&&test_fail_extension_write)return ERROR_ACCESS_DENIED;CopyMemory(target,bytes,size);return ERROR_SUCCESS;}
static LSTATUS WINAPI fixture_delete_value(HKEY key,LPCWSTR name)
{(void)key;WCHAR *target=!wcscmp(name,L"directory_identity")?test_directory_identity:!wcscmp(name,L"extension_identity")?test_extension_identity:NULL;assert(target);if(!*target)return ERROR_FILE_NOT_FOUND;*target=0;return ERROR_SUCCESS;}
static LSTATUS WINAPI fixture_delete_empty_key(HKEY key,LPCWSTR name)
{
    assert(key==(HKEY)(ULONG_PTR)124&&name==NULL);test_empty_deletes++;
    if(test_inject_foreign_at_delete==1)test_foreign_value=TRUE;
    if(test_inject_foreign_at_delete==2)test_foreign_subkey=TRUE;
    if(*test_directory_identity||*test_extension_identity||test_path_only||test_foreign_value||test_foreign_subkey)return ERROR_DIR_NOT_EMPTY;
    test_record_exists=FALSE;return ERROR_SUCCESS;
}
static UINT WINAPI fixture_get_property(MSIHANDLE handle,LPCWSTR name,LPWSTR value,LPDWORD size)
{assert(handle==1||handle==2);assert(!wcscmp(name,L"CustomActionData")||!wcscmp(name,L"HostFolder"));const WCHAR *source=!wcscmp(name,L"CustomActionData")&&*test_cad?test_cad:test_folder;DWORD length=(DWORD)wcslen(source);if(*size<=length)return ERROR_MORE_DATA;CopyMemory(value,source,(length+1)*sizeof(WCHAR));*size=length;return ERROR_SUCCESS;}
static LONG WINAPI fixture_trust(HWND window,GUID *policy,LPVOID data){(void)window;(void)policy;(void)data;return ERROR_SUCCESS;}
static INT_PTR WINAPI fixture_dialog(HINSTANCE module,LPCWSTR resource,HWND parent,DLGPROC callback,LPARAM argument)
{(void)module;(void)resource;(void)parent;(void)callback;(void)argument;test_dialogs++;return ERROR_INSTALL_USEREXIT;}
static BOOL WINAPI fixture_launch(LPCWSTR app,LPWSTR args,LPSECURITY_ATTRIBUTES process,LPSECURITY_ATTRIBUTES thread,BOOL inherit,DWORD flags,LPVOID environment,LPCWSTR cwd,LPSTARTUPINFOW startup,LPPROCESS_INFORMATION output)
{(void)app;(void)args;(void)process;(void)thread;(void)inherit;(void)flags;(void)environment;(void)cwd;(void)startup;(void)output;test_launches++;return FALSE;}
static UINT WINAPI fixture_property(MSIHANDLE handle,LPCWSTR name,LPCWSTR value)
{
    assert(handle==1||handle==2);
    if(!wcscmp(name,L"OaHostFound"))wcscpy_s(test_found,4,value);
    else if(!wcscmp(name,L"OaHostTaken"))wcscpy_s(test_taken,4,value);
    else if(!wcscmp(name,L"HostFolder"))wcscpy_s(test_folder,32768,value);
    else if(!wcscmp(name,L"OaRemovalRegistrationOwned"))wcscpy_s(test_removed,4,value);
    else if(!wcscmp(name,L"RollbackPreparedTarget"))wcscpy_s(test_rollback,32771,value);
    else assert(0);return ERROR_SUCCESS;
}
static MSIHANDLE WINAPI fixture_record(UINT fields){assert(fields==0);return 99;}
static UINT WINAPI fixture_record_string(MSIHANDLE handle,UINT field,LPCWSTR text){assert(handle==99&&field==0&&wcsstr(text,L"saved selection"));return ERROR_SUCCESS;}
static int WINAPI fixture_message(MSIHANDLE install,INSTALLMESSAGE kind,MSIHANDLE record){assert((install==1||install==2)&&kind==INSTALLMESSAGE_ERROR&&record==99);return 0;}
static UINT WINAPI fixture_close_record(MSIHANDLE record){assert(record==99);return ERROR_SUCCESS;}
#define SHGetKnownFolderPath fixture_known_folder
#define RegOpenCurrentUser fixture_open_user
#define RegCloseKey fixture_close_key
#define RegGetValueW fixture_registry
#define RegCreateKeyExW fixture_create_key
#define RegOpenKeyExW fixture_open_key
#define RegSetValueExW fixture_set_value
#define RegDeleteValueW fixture_delete_value
#define SHDeleteEmptyKeyW fixture_delete_empty_key
#define WinVerifyTrust fixture_trust
#define DialogBoxParamW fixture_dialog
#define CreateProcessW fixture_launch
#define MsiSetPropertyW fixture_property
#define MsiGetPropertyW fixture_get_property
#define MsiCreateRecord fixture_record
#define MsiRecordSetStringW fixture_record_string
#define MsiProcessMessage fixture_message
#define MsiCloseHandle fixture_close_record
#define OpenProcessToken fixture_process_token
#define wWinMain fixture_helper_main
#include "prepare_instance.c"
#include "hostfolder_action.c"
#undef wWinMain
static void fixture_write(const WCHAR *path,const void *bytes,DWORD size)
{HANDLE file=CreateFileW(path,GENERIC_WRITE,0,NULL,CREATE_ALWAYS,FILE_ATTRIBUTE_NORMAL,NULL);assert(file!=INVALID_HANDLE_VALUE);DWORD written;assert(WriteFile(file,bytes,size,&written,NULL)&&written==size);CloseHandle(file);}
static void fixture_selection(const WCHAR *path,COMFY_SELECTION *selection)
{
    char *bytes=calloc(SELECTION_LIMIT,1),escaped[1600];assert(bytes);size_t used=0;bytes[used++]='{';
    for(size_t i=0;i<16;i++){assert(escape_json(selection->fields[i],escaped,sizeof(escaped)));int count=sprintf_s(bytes+used,SELECTION_LIMIT-used,"%s\"%s\":\"%s\"",i?",":"",selection_keys[i],escaped);assert(count>0);used+=(size_t)count;}
    bytes[used++]='}';fixture_write(path,bytes,(DWORD)used);free(bytes);
}
static int fixture_publish(COMFY_CONTEXT *context,const WCHAR *mode,const WCHAR *id)
{
    OA_COMFY_INSTANCE *item=calloc(1,sizeof(*item));COMFY_FILE record={0};WCHAR *folder=NULL;COMFY_SHOWN shown;int ok=0;
    if(item&&resolve_instance(context,mode,id,item,&record,&folder)&&shown_record(&record,&shown))ok=publish_selection(context,mode,id,&shown);
    free(item);free(folder);close_file(&record);return ok;
}
static void removal_receiving(const WCHAR *root)
{
    unsigned prior_deletes=test_empty_deletes;
    WCHAR *nodes=calloc(32768,sizeof(WCHAR)),*extension=calloc(32768,sizeof(WCHAR)),*saved=calloc(32768,sizeof(WCHAR));
    assert(nodes&&extension&&saved);swprintf_s(nodes,32768,L"%s\\ComfyUI\\custom_nodes",root);
    swprintf_s(extension,32768,L"%s\\comfyui-openabstractions",nodes);swprintf_s(saved,32768,L"%s.saved",extension);
    wcscpy_s(test_folder,32768,nodes);assert(CreateDirectoryW(extension,NULL));
    assert(ValidateRecordedTarget(1)==ERROR_INSTALL_FAILURE); /* Missing ownership cannot adopt existing files. */
    assert(PrepareTargetRollback(1)==ERROR_SUCCESS&&test_rollback[0]==L'A');
    assert(RecordPreparedTarget(1)==ERROR_SUCCESS&&test_record_exists);
    wcscpy_s(test_cad,32771,test_rollback);
    assert(RollbackPreparedTarget(1)==ERROR_SUCCESS&&!test_record_exists&&!*test_directory_identity&&!*test_extension_identity);*test_cad=0;
    for(unsigned foreign=1;foreign<=2;foreign++){
        assert(PrepareTargetRollback(1)==ERROR_SUCCESS&&test_rollback[0]==L'A');
        assert(RecordPreparedTarget(1)==ERROR_SUCCESS);test_inject_foreign_at_delete=foreign;
        wcscpy_s(test_cad,32771,test_rollback);
        assert(RollbackPreparedTarget(1)==ERROR_INSTALL_FAILURE&&test_record_exists);
        assert(foreign==1?test_foreign_value:test_foreign_subkey);
        *test_cad=0;test_inject_foreign_at_delete=0;test_foreign_value=FALSE;test_foreign_subkey=FALSE;test_record_exists=FALSE;
    }
    assert(test_empty_deletes==prior_deletes+3);
    test_path_only=TRUE;test_record_exists=TRUE;
    assert(RecordPreparedTarget(1)==ERROR_INSTALL_FAILURE);test_path_only=FALSE;
    assert(PrepareTargetRollback(1)==ERROR_SUCCESS&&test_rollback[0]==L'N');
    assert(RecordPreparedTarget(1)==ERROR_SUCCESS);
    wcscpy_s(test_cad,32771,test_rollback);
    assert(RollbackPreparedTarget(1)==ERROR_SUCCESS&&test_record_exists&&!*test_directory_identity&&!*test_extension_identity);*test_cad=0;
    /* A failure between the two writes must roll back the first value too. */
    assert(PrepareTargetRollback(1)==ERROR_SUCCESS);test_fail_extension_write=TRUE;
    assert(RecordPreparedTarget(1)==ERROR_INSTALL_FAILURE&&*test_directory_identity&&!*test_extension_identity);
    test_fail_extension_write=FALSE;wcscpy_s(test_cad,32771,test_rollback);
    assert(RollbackPreparedTarget(1)==ERROR_SUCCESS&&!*test_directory_identity&&!*test_extension_identity);*test_cad=0;
    assert(RecordPreparedTarget(1)==ERROR_SUCCESS&&ValidateRecordedTarget(1)==ERROR_SUCCESS);
    assert(PrepareTargetRollback(1)==ERROR_SUCCESS&&test_rollback[0]==L'P');
    wcscpy_s(test_cad,32771,test_rollback);
    assert(RollbackPreparedTarget(1)==ERROR_SUCCESS);*test_cad=0;
    assert(ValidateRecordedTarget(1)==ERROR_SUCCESS);
    /* An altered record is foreign; rollback must preserve both values. */
    WCHAR original[49];wcscpy_s(original,49,test_directory_identity);test_directory_identity[0]=L'X';
    test_rollback[0]=L'A';wcscpy_s(test_cad,32771,test_rollback);
    assert(RollbackPreparedTarget(1)==ERROR_SUCCESS&&test_directory_identity[0]==L'X'&&*test_extension_identity);
    wcscpy_s(test_directory_identity,49,original);*test_cad=0;
    WCHAR *helper=calloc(32768,sizeof(WCHAR)),*program=calloc(32768,sizeof(WCHAR)),*manifest=calloc(32768,sizeof(WCHAR));
    char *escaped=calloc(32768,1),*metadata=calloc(65536,1);assert(helper&&program&&manifest&&escaped&&metadata);
    swprintf_s(helper,32768,L"%s\\helper",extension);assert(CreateDirectoryW(helper,NULL));
    swprintf_s(program,32768,L"%s\\python.exe",helper);fixture_write(program,"inert helper; never executed",sizeof("inert helper; never executed")-1);
    swprintf_s(manifest,32768,L"%s\\application-manifest.json",extension);
    assert(escape_json(program,escaped,32768));
    int count=sprintf_s(metadata,65536,"{\"name\":\"comfyui\",\"program\":\"%s\"}",escaped);assert(count>0);fixture_write(manifest,metadata,(DWORD)count);
    /* There is no guided selection yet. A later uninstall requests the current
       entry by name using only this exact installed helper and recorded tree. */
    assert(CheckRemovalRegistration(1)==ERROR_SUCCESS&&!wcscmp(test_removed,L"1"));
    /* The existing portable descriptor names this exact private helper.
       Both the stock installer guard and the modern staged-pack guard read it. */
    const char portable[]="{\"name\":\"comfyui\",\"program\":\"{app}/helper/python.exe\"}";
    fixture_write(manifest,portable,sizeof(portable)-1);
    assert(registration_at(nodes));
    assert(CheckRemovalRegistration(1)==ERROR_SUCCESS&&!wcscmp(test_removed,L"1"));
    const char escaped_program[]="{\"name\":\"comfyui\",\"program\":\"{app}/../helper/python.exe\"}";
    fixture_write(manifest,escaped_program,sizeof(escaped_program)-1);
    assert(!registration_at(nodes));
    const char unknown_program[]="{\"name\":\"comfyui\",\"program\":\"{local_app_data}/helper/python.exe\"}";
    fixture_write(manifest,unknown_program,sizeof(unknown_program)-1);
    assert(!registration_at(nodes));
    count=sprintf_s(metadata,65536,"{\"name\":\"foreign\",\"program\":\"%s\"}",escaped);assert(count>0);fixture_write(manifest,metadata,(DWORD)count);
    assert(CheckRemovalRegistration(1)==ERROR_SUCCESS&&!*test_removed);
    assert(MoveFileExW(extension,saved,0)&&CreateDirectoryW(extension,NULL));
    wcscpy_s(test_cad,32771,test_rollback);
    assert(RollbackPreparedTarget(1)==ERROR_SUCCESS&&!wcscmp(test_directory_identity,original)&&*test_extension_identity);*test_cad=0;
    assert(ValidateRecordedTarget(1)==ERROR_INSTALL_FAILURE&&RecordPreparedTarget(1)==ERROR_INSTALL_FAILURE);
    assert(CheckRemovalRegistration(1)==ERROR_INSTALL_FAILURE&&!*test_removed);
    assert(RemoveDirectoryW(extension)&&MoveFileExW(saved,extension,0));
    assert(ValidateRecordedTarget(1)==ERROR_SUCCESS);
    /* A real own-tree directory symlink, where Developer Mode permits it,
       exercises reparse refusal without reaching the external sentinel. */
    WCHAR *outside=calloc(32768,sizeof(WCHAR)),*sentinel=calloc(32768,sizeof(WCHAR));assert(outside&&sentinel);
    swprintf_s(outside,32768,L"%s\\external-removal-sentinel",root);
    if(!CreateDirectoryW(outside,NULL))assert(GetLastError()==ERROR_ALREADY_EXISTS);
    swprintf_s(sentinel,32768,L"%s\\keep.txt",outside);fixture_write(sentinel,"keep",4);
    assert(MoveFileExW(extension,saved,0));
    WCHAR *junction=calloc(32768,sizeof(WCHAR));assert(junction);swprintf_s(junction,32768,L"%s\\fixture-reparse-junction",root);
    DWORD attributes=GetFileAttributesW(junction);
    if(attributes!=INVALID_FILE_ATTRIBUTES&&(attributes&FILE_ATTRIBUTE_REPARSE_POINT)){
        assert(MoveFileExW(junction,extension,0));
        wcscpy_s(test_cad,32771,test_rollback);
        assert(RollbackPreparedTarget(1)==ERROR_SUCCESS&&!wcscmp(test_directory_identity,original)&&*test_extension_identity);*test_cad=0;
        assert(ValidateRecordedTarget(1)==ERROR_INSTALL_FAILURE&&RecordPreparedTarget(1)==ERROR_INSTALL_FAILURE);
        assert(CheckRemovalRegistration(1)==ERROR_INSTALL_FAILURE&&!*test_removed);
        assert(GetFileAttributesW(sentinel)!=INVALID_FILE_ATTRIBUTES);
        assert(MoveFileExW(extension,junction,0));
        puts("PASS: actual owned directory junction refused; external sentinel preserved");
    }else if(CreateSymbolicLinkW(extension,outside,SYMBOLIC_LINK_FLAG_DIRECTORY|SYMBOLIC_LINK_FLAG_ALLOW_UNPRIVILEGED_CREATE)){
        wcscpy_s(test_cad,32771,test_rollback);
        assert(RollbackPreparedTarget(1)==ERROR_SUCCESS&&!wcscmp(test_directory_identity,original)&&*test_extension_identity);*test_cad=0;
        assert(ValidateRecordedTarget(1)==ERROR_INSTALL_FAILURE&&RecordPreparedTarget(1)==ERROR_INSTALL_FAILURE);
        assert(GetFileAttributesW(sentinel)!=INVALID_FILE_ATTRIBUTES);assert(RemoveDirectoryW(extension));
    }else printf("SKIP: actual directory symlink creation unavailable (Windows error %lu); no reparse execution claim\n",GetLastError());
    assert(MoveFileExW(saved,extension,0)&&ValidateRecordedTarget(1)==ERROR_SUCCESS);
    assert(DeleteFileW(sentinel)&&RemoveDirectoryW(outside));
    assert(DeleteFileW(program)&&DeleteFileW(manifest)&&RemoveDirectoryW(helper)&&RemoveDirectoryW(extension));
    test_record_exists=FALSE;*test_directory_identity=0;*test_extension_identity=0;
    free(nodes);free(extension);free(saved);free(outside);free(sentinel);free(helper);free(program);free(manifest);free(escaped);free(metadata);free(junction);
}
static void preparation_receiving(const WCHAR *root)
{
    test_root=root;WCHAR guided[]=L"--guided",verify[]=L"--verify-only";
    removal_receiving(root);
    assert(fixture_helper_main(NULL,NULL,verify,0)==ERROR_INSTALL_FAILURE);
    assert(fixture_helper_main(NULL,NULL,guided,0)==ERROR_INSTALL_USEREXIT);
    assert(test_dialogs==1&&test_launches==0);
    COMFY_CONTEXT *context=calloc(1,sizeof(*context));assert(context&&context_open(context,FALSE));
    /* Actual ordinary token facts remain the receiver identity. A service
       primary is accepted only with that checked current impersonation. */
    assert(ImpersonateSelf(SecurityImpersonation));HANDLE thread=NULL;TOKEN_FACTS originalUser={0},service;
    assert(OpenThreadToken(GetCurrentThread(),TOKEN_QUERY,TRUE,&thread)&&token_facts(thread,&originalUser)&&ordinary_user(&originalUser));CloseHandle(thread);assert(RevertToSelf());
    service=originalUser;DWORD systemSize=sizeof(service.sid);assert(CreateWellKnownSid(WinLocalSystemSid,NULL,service.sid,&systemSize));service.service=TRUE;service.elevated=TRUE;service.integrity=SECURITY_MANDATORY_SYSTEM_RID;
    assert(receiving_user(&service,&originalUser,TRUE));assert(!receiving_user(&service,&originalUser,FALSE));assert(!receiving_user(&service,&service,TRUE));
    OA_COMFY_INSTANCE *shownItem=calloc(1,sizeof(*shownItem));COMFY_FILE displayed={0};WCHAR *shownFolder=NULL;COMFY_SHOWN shown;
    assert(shownItem&&resolve_instance(context,L"standalone",L"one",shownItem,&displayed,&shownFolder)&&shown_record(&displayed,&shown));
    char *displayedBytes=calloc(displayed.size+1,1);assert(displayedBytes);memcpy(displayedBytes,displayed.bytes,displayed.size);size_t displayedSize=displayed.size;close_file(&displayed);free(shownItem);free(shownFolder);
    WCHAR *displayedPath=calloc(32768,sizeof(WCHAR));assert(displayedPath);swprintf_s(displayedPath,32768,L"%s\\%s",root,OA_COMFY_RECORDS);
    char *shownName=strstr(displayedBytes,"fixture");assert(shownName);*shownName='F';fixture_write(displayedPath,displayedBytes,(DWORD)displayedSize);
    assert(!publish_selection(context,L"standalone",L"one",&shown)); /* Same ID, changed displayed name refuses. */
    *shownName='f';fixture_write(displayedPath,displayedBytes,(DWORD)displayedSize);free(displayedBytes);free(displayedPath);
    assert(GetFileAttributesW(context->selection)==INVALID_FILE_ATTRIBUTES);
    {OA_NATIVE_FILE parent={0};int directoryReady=ensure_directory(context,&parent);DWORD directoryError=GetLastError();oa_native_close(&parent);fprintf(stderr,"fixture selection parent ready=%d error=%lu\n",directoryReady,directoryError);assert(directoryReady);}
    {int published=fixture_publish(context,L"standalone",L"one");if(!published)fprintf(stderr,"fixture publish error=%lu\n",GetLastError());assert(published);}
    assert(!fixture_publish(context,L"standalone",L"one")); /* Live parent prevents replacement. */
    assert(fixture_helper_main(NULL,NULL,verify,0)==0);
    wcscpy_s(test_found,4,L"bad");wcscpy_s(test_taken,4,L"bad");wcscpy_s(test_folder,32768,L"C:\\untrusted-property");
    assert(ImpersonateSelf(SecurityImpersonation));test_deny_primary=TRUE;
    assert(FindHostFolder(1)==ERROR_SUCCESS&&!wcscmp(test_found,L"1")&&!*test_taken&&wcscmp(test_folder,L"C:\\untrusted-property"));
    test_deny_primary=FALSE;assert(RevertToSelf());assert(test_denied_primary_queries==0);
    /* Distinct case-sensitive children must not satisfy the execute receiver's
       captured HostFolder, even when the spelling folds to the selected name. */
    WCHAR *caseParent=calloc(32768,sizeof(WCHAR)),*caseTwin=calloc(32768,sizeof(WCHAR));assert(caseParent&&caseTwin);
    swprintf_s(caseParent,32768,L"%s\\backend\\ComfyUI",root);swprintf_s(caseTwin,32768,L"%s\\CUSTOM_NODES",caseParent);
    HANDLE caseHandle=CreateFileW(caseParent,FILE_WRITE_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE,NULL,OPEN_EXISTING,FILE_FLAG_BACKUP_SEMANTICS,NULL);
    DWORD caseSensitive=1;assert(caseHandle!=INVALID_HANDLE_VALUE&&SetFileInformationByHandle(caseHandle,(FILE_INFO_BY_HANDLE_CLASS)23,&caseSensitive,sizeof(caseSensitive)));CloseHandle(caseHandle);
    assert(CreateDirectoryW(caseTwin,NULL));swprintf_s(caseParent,32768,L"%s\\comfyui-openabstractions",test_folder);assert(CreateDirectoryW(caseParent,NULL));wcscpy_s(test_cad,32771,caseTwin);
    assert(ValidatePreparedHost(1)==ERROR_INSTALL_FAILURE);*test_cad=0;
    assert(ValidatePreparedHost(1)==ERROR_SUCCESS);assert(RemoveDirectoryW(caseParent));assert(RemoveDirectoryW(caseTwin));free(caseTwin);free(caseParent);
    COMFY_FILE original={0};assert(open_file(context->selection,SELECTION_LIMIT,FALSE,&original));
    char *saved=malloc(original.size);assert(saved);memcpy(saved,original.bytes,original.size);DWORD savedSize=(DWORD)original.size;
    WCHAR *other=calloc(32768,sizeof(WCHAR));assert(other);swprintf_s(other,32768,L"%s.other",context->selection);
    assert(!MoveFileExW(context->selection,other,0)); /* Stable handle forbids replacement. */
    close_file(&original);
    COMFY_SELECTION *selection=calloc(1,sizeof(*selection));COMFY_FILE changedSelection={0};assert(selection&&open_file(context->selection,SELECTION_LIMIT,FALSE,&changedSelection)&&strict_selection(&changedSelection,selection));close_file(&changedSelection);
    wcscpy_s(selection->fields[0],257,L"2");fixture_selection(context->selection,selection);
    assert(!fixture_publish(context,L"standalone",L"one"));assert(FindHostFolder(2)==ERROR_INSTALL_FAILURE&&!*test_found&&!*test_folder);
    fixture_write(context->selection,saved,savedSize);assert(open_file(context->selection,SELECTION_LIMIT,FALSE,&changedSelection)&&strict_selection(&changedSelection,selection));close_file(&changedSelection);
    ULONGLONG old=now_time()-SELECTION_LIFETIME-10000000;swprintf_s(selection->fields[2],257,L"%llu",old);swprintf_s(selection->fields[3],257,L"%llu",old+SELECTION_LIFETIME);fixture_selection(context->selection,selection);
    assert(fixture_helper_main(NULL,NULL,verify,0)==ERROR_INSTALL_FAILURE);assert(!fixture_publish(context,L"standalone",L"one")); /* Expiry never steals a live owner's file. */
    removal_receiving(root); /* Actual expired selection cannot block owned uninstall/Panel request. */
    fixture_write(context->selection,saved,savedSize);assert(open_file(context->selection,SELECTION_LIMIT,FALSE,&changedSelection)&&strict_selection(&changedSelection,selection));close_file(&changedSelection);
    wcscpy_s(selection->fields[6],257,L"S-1-5-21-9");fixture_selection(context->selection,selection);assert(FindHostFolder(2)==ERROR_INSTALL_FAILURE&&!*test_found&&!*test_folder);
    fixture_write(context->selection,saved,savedSize);
    assert(CreateHardLinkW(other,context->selection,NULL));assert(fixture_helper_main(NULL,NULL,verify,0)==ERROR_INSTALL_FAILURE);assert(!fixture_publish(context,L"standalone",L"one"));assert(DeleteFileW(other));
    assert(open_file(context->selection,SELECTION_LIMIT,FALSE,&changedSelection)&&strict_selection(&changedSelection,selection));close_file(&changedSelection);
    swprintf_s(selection->fields[5],257,L"%llu",context->parent_created+1);fixture_selection(context->selection,selection);
    assert(fixture_publish(context,L"standalone",L"one")); /* Proven PID creation mismatch retires the exact checked file. */
    WCHAR *foreign=calloc(32768,sizeof(WCHAR));assert(foreign);swprintf_s(foreign,32768,L"%s\\backend\\ComfyUI\\custom_nodes\\comfyui-openabstractions",root);assert(CreateDirectoryW(foreign,NULL));
    assert(FindHostFolder(2)==ERROR_SUCCESS&&!wcscmp(test_found,L"1")&&!wcscmp(test_taken,L"1"));assert(RemoveDirectoryW(foreign));free(foreign);
    free(selection);free(saved);free(other);
    /* The execute receiver must independently refuse current official drift. */
    WCHAR *record=calloc(32768,sizeof(WCHAR));assert(record);swprintf_s(record,32768,L"%s\\%s",root,OA_COMFY_RECORDS);
    HANDLE changed=CreateFileW(record,GENERIC_WRITE,0,NULL,OPEN_EXISTING,0,NULL);assert(changed!=INVALID_HANDLE_VALUE);
    DWORD written;assert(WriteFile(changed,"[]",2,&written,NULL)&&SetEndOfFile(changed));CloseHandle(changed);free(record);
    assert(FindHostFolder(2)==ERROR_INSTALL_FAILURE&&!*test_found&&!*test_taken&&!*test_folder);
    assert(test_launches==0);context_close(context);free(context);
    puts("PASS: actual helper verify/cancel/publish, live concurrency and both MSI receiving calls; registry/trust/UI/process APIs are inert fixture replacements");
}
#endif

static OA_COMFY_INSTANCE instance;
static const char *ready="[{\"id\":\"one\",\"name\":\"Render \\u03a9\",\"sourceId\":\"standalone\",\"status\":\"installed\",\"installPath\":\"D:\\\\GPU\"}]";
static int parse(const char *text,const WCHAR *id) {return oa_comfy_instance(text,strlen(text),id,&instance);}
static DWORD WINAPI run_fixture(void *argument)
{
    const WCHAR *fixture=argument; /* Only the caller-owned fixture directory is read. */
    assert(parse(ready,L"one")==1);
    assert(!wcscmp(instance.name,L"Render \x03a9"));
    assert(!wcscmp(instance.base,L"D:\\GPU\\backend\\ComfyUI"));
    assert(!wcscmp(instance.source,L"D:\\GPU\\backend\\ComfyUI\\main.py"));
    assert(!wcscmp(instance.python,L"D:\\GPU\\backend\\standalone-env\\python.exe"));
    assert(parse(ready,L"absent")==0);
    assert(parse("[]",L"one")==0);
    assert(parse("{}",L"one")==-1);
    assert(parse("[{\"id\":\"one\",\"name\":\"x\",\"sourceId\":\"standalone\",\"status\":\"installing\",\"installPath\":\"C:\\\\x\"}]",L"one")==2);
    assert(parse("[{\"id\":\"one\",\"name\":\"x\",\"sourceId\":\"cloud\",\"status\":\"installed\",\"installPath\":\"C:\\\\x\"}]",L"one")==2);
    assert(parse("[{\"id\":\"one\",\"name\":\"x\",\"sourceId\":\"standalone\"},{\"id\":\"one\",\"name\":\"y\",\"sourceId\":\"standalone\"}]",L"absent")==-1);
    const char *bad[]={"[{},]","[1]","[{\"id\":\"a\",\"name\":\"x\",\"sourceId\":\"standalone\",\"x\":1..2}]","[] trailing","[", "[{\"id\":\"a\\u0000b\",\"name\":\"x\",\"sourceId\":\"standalone\"}]"};
    for(size_t i=0;i<sizeof(bad)/sizeof(bad[0]);i++) assert(parse(bad[i],L"one")==-1);
    assert(oa_comfy_instance("[]",MAX_CONFIG+1,L"one",&instance)==-1);
    char *many=calloc(100000,1); assert(many); strcpy_s(many,100000,"[");
    for(int i=0;i<257;i++){char entry[200];sprintf_s(entry,sizeof(entry),"%s{\"id\":\"%d\",\"name\":\"x\",\"sourceId\":\"standalone\"}",i?",":"",i);strcat_s(many,100000,entry);}
    strcat_s(many,100000,"]");assert(parse(many,L"absent")==-1);free(many);
    assert(parse("[{\"id\":\"old\",\"name\":\"Adopted\",\"sourceId\":\"standalone\",\"status\":\"installed\",\"installPath\":\"E:\\\\wrapper\",\"adopted\":true,\"adoptedBaseDir\":\"D:\\\\legacy\",\"adoptedPythonPath\":\"D:\\\\legacy\\\\.venv\\\\Scripts\\\\python.exe\"}]",L"old")==1);
    assert(instance.adopted && !wcscmp(instance.base,L"D:\\legacy"));
    assert(!wcscmp(instance.source,L"E:\\wrapper\\ComfyUI\\main.py"));
    assert(parse("[{\"id\":\"old\",\"name\":\"x\",\"sourceId\":\"standalone\",\"status\":\"installed\",\"installPath\":\"D:\\\\x\",\"adopted\":true}]",L"old")==-1);
    const char *legacy="{\"basePath\":\"D:\\\\legacy\"}";
    assert(parse(legacy,L"legacy")==-1);
    assert(oa_comfy_legacy(legacy,strlen(legacy),&instance));
    assert(!wcscmp(instance.id,L"legacy"));
    ZeroMemory(&instance,sizeof(instance));instance.ready=TRUE;
    swprintf_s(instance.base,32768,L"%s\\ComfyUI",fixture);
    swprintf_s(instance.source,32768,L"%s\\main.py",instance.base);
    swprintf_s(instance.python,32768,L"%s\\.venv\\Scripts\\python.exe",instance.base);
    WCHAR *folder=NULL;assert(oa_comfy_paths(&instance,&folder));free(folder);
    WCHAR *separator=wcsrchr(instance.python,L'\\');assert(separator);*separator=L'/';
    assert(!oa_comfy_plain(instance.python,FALSE)); /* File exists; mixed separator itself must refuse. */
    wcscpy_s(instance.python,32768,L"\\\\server\\share\\python.exe");assert(!oa_comfy_paths(&instance,&folder));
    swprintf_s(instance.python,32768,L"%s\\missing.exe",instance.base);assert(!oa_comfy_paths(&instance,&folder));
    swprintf_s(instance.python,32768,L"%s\\link/python.exe",instance.base);assert(!oa_comfy_paths(&instance,&folder));
    assert(!wcscmp(OA_COMFY_RECORDS,L"Comfy Desktop\\installations.json"));
    assert(oa_comfy_read(fixture,L"one",&instance)==1);
    assert(oa_comfy_read(fixture,L"absent",&instance)==0);
    /* Exercise the shared generic consumer functions as well. */
    assert(!same_folder(L"C:\\",L"",L"fixture"));
#ifdef OA_COMFY_PREPARATION_TESTS
    preparation_receiving(fixture);
#endif
    puts("PASS: managed, adopted, explicit legacy, Unicode/custom drive, duplicate/malformed/bounds/readiness and fixture filesystem");
    return 0;
}
int wmain(int argc,WCHAR **argv)
{
    assert(argc==2);
    HANDLE thread=CreateThread(NULL,64*1024,run_fixture,argv[1],STACK_SIZE_PARAM_IS_A_RESERVATION,NULL);
    assert(thread);
    assert(WaitForSingleObject(thread,15000)==WAIT_OBJECT_0);
    DWORD code=1;assert(GetExitCodeThread(thread,&code));CloseHandle(thread);
    return (int)code;
}

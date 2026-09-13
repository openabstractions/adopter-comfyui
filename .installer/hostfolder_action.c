/* Setup derives the current target; removal uses recorded directory identity.
   Selection JSON is current-user configuration, never installed ownership. */
#define OA_COMFY_ACTION_ONLY
#include "prepare_instance.c"
#include <msiquery.h>
#include <shlwapi.h>

/* The existing private MSI record owns the installed target. FileIdInfo comes
 * from the shared host-folder reader; removal never needs a live interpreter. */
static int installed_target(const WCHAR *folder,BOOL capture)
{
    const WCHAR *key=L"Software\\OpenAbstractions\\Extensions\\comfyui";
    WCHAR *child=calloc(32768,sizeof(WCHAR));HKEY user=NULL,record=NULL;
    FILE_ID_INFO parent_id,child_id;WCHAR parent[49],extension[49],saved_parent[49],saved_extension[49];
    DWORD kind=0,size=sizeof(saved_parent),disposition=0;int ok=0;
    if(!child||!oa_comfy_plain(folder,TRUE)||!folder_id(folder,&parent_id)||
       FAILED(StringCchPrintfW(child,32768,L"%s\\comfyui-openabstractions",folder))||
       !oa_comfy_plain(child,TRUE)||!folder_id(child,&child_id)||RegOpenCurrentUser(capture?(KEY_READ|KEY_WRITE):KEY_READ,&user)!=ERROR_SUCCESS)goto done;
    identity_text(&parent_id,parent);identity_text(&child_id,extension);
    if(capture){
        if(RegCreateKeyExW(user,key,0,NULL,0,KEY_QUERY_VALUE|KEY_SET_VALUE,NULL,&record,&disposition)!=ERROR_SUCCESS)goto done;
    }else if(RegOpenKeyExW(user,key,0,KEY_QUERY_VALUE,&record)!=ERROR_SUCCESS)goto done;
    LSTATUS state=RegGetValueW(record,NULL,L"directory_identity",RRF_RT_REG_SZ,&kind,saved_parent,&size);
    if(state==ERROR_SUCCESS){
        size=sizeof(saved_extension);
        if(RegGetValueW(record,NULL,L"extension_identity",RRF_RT_REG_SZ,&kind,saved_extension,&size)!=ERROR_SUCCESS||
           wcscmp(parent,saved_parent)||wcscmp(extension,saved_extension))goto done;
    }else{
        /* A historical path-only record cannot acquire a replacement identity. */
        DWORD existing=0;
        if(!capture||state!=ERROR_FILE_NOT_FOUND||RegGetValueW(record,NULL,L"folder",RRF_RT_REG_SZ,NULL,NULL,&existing)!=ERROR_FILE_NOT_FOUND)goto done;
        if(RegSetValueExW(record,L"directory_identity",0,REG_SZ,(BYTE*)parent,sizeof(parent))!=ERROR_SUCCESS||
           RegSetValueExW(record,L"extension_identity",0,REG_SZ,(BYTE*)extension,sizeof(extension))!=ERROR_SUCCESS)goto done;
    }
    ok=1;
done:if(record)RegCloseKey(record);if(user)RegCloseKey(user);free(child);return ok;
}

static int plain_descendants(const WCHAR *folder,size_t *remaining,unsigned depth)
{
    WCHAR *pattern=calloc(32768,sizeof(WCHAR)),*child=calloc(32768,sizeof(WCHAR));WIN32_FIND_DATAW data;HANDLE find=INVALID_HANDLE_VALUE;int ok=0;
    if(!pattern||!child||depth>MAX_DEPTH||!oa_comfy_plain(folder,TRUE)||FAILED(StringCchPrintfW(pattern,32768,L"%s\\*",folder)))goto done;
    find=FindFirstFileW(pattern,&data);
    if(find==INVALID_HANDLE_VALUE){ok=GetLastError()==ERROR_FILE_NOT_FOUND;goto done;}
    do{
        if(!wcscmp(data.cFileName,L".")||!wcscmp(data.cFileName,L".."))continue;
        if(!*remaining||--*remaining==0||(data.dwFileAttributes&FILE_ATTRIBUTE_REPARSE_POINT)||
           FAILED(StringCchPrintfW(child,32768,L"%s\\%s",folder,data.cFileName)))goto done;
        if((data.dwFileAttributes&FILE_ATTRIBUTE_DIRECTORY)&&!plain_descendants(child,remaining,depth+1))goto done;
    }while(FindNextFileW(find,&data));
    ok=GetLastError()==ERROR_NO_MORE_FILES;
done:if(find!=INVALID_HANDLE_VALUE)FindClose(find);free(pattern);free(child);return ok;
}

__declspec(dllexport) UINT __stdcall RecordPreparedTarget(MSIHANDLE install)
{
    WCHAR *folder=calloc(32768,sizeof(WCHAR));DWORD size=32768;UINT result=ERROR_INSTALL_FAILURE;
    if(folder&&MsiGetPropertyW(install,L"CustomActionData",folder,&size)==ERROR_SUCCESS&&size){
        while(size>3&&folder[size-1]==L'\\')folder[--size]=0;
        if(installed_target(folder,TRUE))result=ERROR_SUCCESS;
    }
    free(folder);return result;
}

/* Only the new identity pair is written before the standard MSI registry
 * action. The transaction carries its prior-presence state in rollback CAD;
 * A means absent key, N a preexisting key without identity values, and P an
 * existing pair validated by RecordPreparedTarget and never overwritten. */
__declspec(dllexport) UINT __stdcall PrepareTargetRollback(MSIHANDLE install)
{
    HKEY user=NULL,record=NULL;WCHAR parent[49],child[49];DWORD size=sizeof(parent),kind;
    WCHAR *data=calloc(32771,sizeof(WCHAR));UINT result=ERROR_INSTALL_FAILURE;LSTATUS state;
    if(!data||RegOpenCurrentUser(KEY_READ,&user)!=ERROR_SUCCESS)goto done;
    data[0]=L'N';data[1]=L'|';size=32768;
    if(MsiGetPropertyW(install,L"HostFolder",data+2,&size)!=ERROR_SUCCESS||!size)goto done;
    state=RegOpenKeyExW(user,L"Software\\OpenAbstractions\\Extensions\\comfyui",0,KEY_QUERY_VALUE,&record);
    if(state==ERROR_SUCCESS){
        size=sizeof(parent);state=RegGetValueW(record,NULL,L"directory_identity",RRF_RT_REG_SZ,&kind,parent,&size);
        if(state==ERROR_SUCCESS){
            size=sizeof(child);
            if(RegGetValueW(record,NULL,L"extension_identity",RRF_RT_REG_SZ,&kind,child,&size)!=ERROR_SUCCESS)goto done;
            data[0]=L'P';
        }else{
            if(state!=ERROR_FILE_NOT_FOUND)goto done;
            size=sizeof(child);
            if(RegGetValueW(record,NULL,L"extension_identity",RRF_RT_REG_SZ,&kind,child,&size)!=ERROR_FILE_NOT_FOUND)goto done;
            size=0;
            if(RegGetValueW(record,NULL,L"folder",RRF_RT_REG_SZ,NULL,NULL,&size)!=ERROR_FILE_NOT_FOUND)goto done;
        }
    }else if(state==ERROR_FILE_NOT_FOUND)data[0]=L'A';
    else goto done;
    result=MsiSetPropertyW(install,L"RollbackPreparedTarget",data);
done:if(record)RegCloseKey(record);if(user)RegCloseKey(user);free(data);return result;
}

__declspec(dllexport) UINT __stdcall RollbackPreparedTarget(MSIHANDLE install)
{
    WCHAR *data=calloc(32771,sizeof(WCHAR)),*extension=calloc(32768,sizeof(WCHAR));DWORD size=32771,kind;
    HKEY user=NULL,record=NULL;FILE_ID_INFO parent_id,child_id;WCHAR parent[49],child[49],saved[49];
    UINT result=ERROR_SUCCESS;
    /* Rollback preserves a changed/foreign/missing target for recovery. */
    if(!data||!extension||MsiGetPropertyW(install,L"CustomActionData",data,&size)!=ERROR_SUCCESS||
       size<3||data[1]!=L'|'||(data[0]!=L'N'&&data[0]!=L'A'))goto done;
    WCHAR *folder=data+2;size_t length=wcslen(folder);
    while(length>3&&folder[length-1]==L'\\')folder[--length]=0;
    if(!oa_comfy_plain(folder,TRUE)||!folder_id(folder,&parent_id)||
       FAILED(StringCchPrintfW(extension,32768,L"%s\\comfyui-openabstractions",folder))||
       !oa_comfy_plain(extension,TRUE)||!folder_id(extension,&child_id)||
       RegOpenCurrentUser(KEY_READ|KEY_WRITE,&user)!=ERROR_SUCCESS||
       RegOpenKeyExW(user,L"Software\\OpenAbstractions\\Extensions\\comfyui",0,KEY_QUERY_VALUE|KEY_SET_VALUE|DELETE,&record)!=ERROR_SUCCESS)goto done;
    size=0;if(RegGetValueW(record,NULL,L"folder",RRF_RT_REG_SZ,NULL,NULL,&size)!=ERROR_FILE_NOT_FOUND)goto done;
    identity_text(&parent_id,parent);identity_text(&child_id,child);
    const WCHAR *names[]={L"directory_identity",L"extension_identity"};const WCHAR *expected[]={parent,child};
    /* Check the whole present pair before deleting either half. */
    for(unsigned i=0;i<2;i++){
        size=sizeof(saved);LSTATUS state=RegGetValueW(record,NULL,names[i],RRF_RT_REG_SZ,&kind,saved,&size);
        if(state!=ERROR_FILE_NOT_FOUND&&(state!=ERROR_SUCCESS||wcscmp(saved,expected[i])))goto done;
    }
    for(unsigned i=0;i<2;i++){
        size=sizeof(saved);
        if(RegGetValueW(record,NULL,names[i],RRF_RT_REG_SZ,&kind,saved,&size)==ERROR_SUCCESS&&!wcscmp(saved,expected[i])){
            LSTATUS state=RegDeleteValueW(record,names[i]);
            if(state!=ERROR_SUCCESS&&state!=ERROR_FILE_NOT_FOUND)result=ERROR_INSTALL_FAILURE;
        }
    }
    /* The existing Shell API deletes only an empty key. Keeping this captured
     * handle avoids adopting a different key recreated under the same name. */
    if(data[0]==L'A'&&result==ERROR_SUCCESS){
        LSTATUS state=SHDeleteEmptyKeyW(record,NULL);
        if(state!=ERROR_SUCCESS&&state!=ERROR_FILE_NOT_FOUND)result=ERROR_INSTALL_FAILURE;
    }
done:if(record)RegCloseKey(record);if(user)RegCloseKey(user);free(data);free(extension);return result;
}

__declspec(dllexport) UINT __stdcall ValidateRecordedTarget(MSIHANDLE install)
{
    WCHAR *folder=calloc(32768,sizeof(WCHAR)),*child=calloc(32768,sizeof(WCHAR));DWORD size=32768;size_t remaining=100000;UINT result=ERROR_INSTALL_FAILURE;
    if(folder&&child&&MsiGetPropertyW(install,L"CustomActionData",folder,&size)==ERROR_SUCCESS){
        if(!size){size=32768;if(MsiGetPropertyW(install,L"HostFolder",folder,&size)!=ERROR_SUCCESS)goto done;}
        if(!size)goto done;
        while(size>3&&folder[size-1]==L'\\')folder[--size]=0;
        if(installed_target(folder,FALSE)&&SUCCEEDED(StringCchPrintfW(child,32768,L"%s\\comfyui-openabstractions",folder))&&plain_descendants(child,&remaining,0))result=ERROR_SUCCESS;
    }
done:free(folder);free(child);return result;
}

/* Deferred setup and the later Panel request consume the same captured target.
 * Neither an MSI property nor a freshly changed selection may redirect them. */
static int registration_in_pack(const WCHAR *pack)
{
    WCHAR *path=calloc(32768,sizeof(WCHAR)),*program=calloc(32768,sizeof(WCHAR));COMFY_FILE manifest={0},helper={0},registeredHelper={0};int result=0;
    if(!path||!program)goto done;
    if(FAILED(StringCchPrintfW(path,32768,L"%s\\application-manifest.json",pack))||
       !open_file(path,65536,FALSE,&manifest)||!oa_comfy_text(manifest.bytes,manifest.size,"program",program,32768,TRUE))goto done;
    if(!wcscmp(program,L"{app}/helper/python.exe")){
        if(FAILED(StringCchPrintfW(program,32768,L"%s\\helper\\python.exe",pack)))goto done;
    }
    if(!open_file(program,16*1024*1024,FALSE,&registeredHelper)||
       FAILED(StringCchPrintfW(path,32768,L"%s\\helper\\python.exe",pack))||
       !open_file(path,16*1024*1024,FALSE,&helper)||!oa_native_same_handles(helper.handle,registeredHelper.handle))goto done;
    if(!oa_comfy_text(manifest.bytes,manifest.size,"name",program,32768,TRUE)||wcscmp(program,L"comfyui"))goto done;
    result=1;
done:close_file(&manifest);close_file(&helper);close_file(&registeredHelper);free(path);free(program);return result;
}
static int registration_at(const WCHAR *folder)
{
    WCHAR *pack=calloc(32768,sizeof(WCHAR));int result=0;
    if(pack&&SUCCEEDED(StringCchPrintfW(pack,32768,L"%s\\comfyui-openabstractions",folder)))result=registration_in_pack(pack);
    free(pack);return result;
}

static UINT validate_prepared(MSIHANDLE install,BOOL registration)
{
    WCHAR *expected=calloc(32768,sizeof(WCHAR)),*folder=NULL,*path=calloc(32768,sizeof(WCHAR)),*program=calloc(32768,sizeof(WCHAR));
    COMFY_CONTEXT *context=calloc(1,sizeof(*context));COMFY_SELECTION *selection=calloc(1,sizeof(*selection));
    OA_NATIVE_FILE expectedHeld={0},folderHeld={0};
    COMFY_FILE manifest={0},helper={0};UINT result=ERROR_INSTALL_FAILURE;DWORD length=32768;
    if(!expected||!path||!program||!context||!selection)goto done;
    if(MsiGetPropertyW(install,L"CustomActionData",expected,&length)!=ERROR_SUCCESS)goto done;
    if(!length){length=32768;if(MsiGetPropertyW(install,L"HostFolder",expected,&length)!=ERROR_SUCCESS||!length)goto done;}
    if(!context_open(context,TRUE)||!current_selection(context,&folder,selection))goto done;
    size_t a=wcslen(expected),b=wcslen(folder);
    while(a>3&&expected[a-1]==L'\\')expected[--a]=0;
    while(b>3&&folder[b-1]==L'\\')folder[--b]=0;
    if(!oa_native_open(expected,FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE,TRUE,FALSE,&expectedHeld)||
       !oa_native_open(folder,FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE,TRUE,FALSE,&folderHeld)||!oa_native_same_handles(expectedHeld.file,folderHeld.file))goto done;
    if(FAILED(StringCchPrintfW(path,32768,L"%s\\comfyui-openabstractions",folder))||!oa_comfy_plain(path,TRUE))goto done;
    if(!registration){result=ERROR_SUCCESS;goto done;}
    if(!registration_at(folder))goto done;
    result=ERROR_SUCCESS;
done:
    oa_native_close(&expectedHeld);oa_native_close(&folderHeld);close_file(&manifest);close_file(&helper);free(expected);free(folder);free(path);free(program);free(selection);
    if(context){context_close(context);free(context);}return result;
}
__declspec(dllexport) UINT __stdcall ValidatePreparedHost(MSIHANDLE install){return validate_prepared(install,FALSE);}
__declspec(dllexport) UINT __stdcall ValidateRegistrationHost(MSIHANDLE install){return validate_prepared(install,TRUE);}
/* Removal asks the same person-confirmed Panel page only while the exact
 * prepared helper still belongs to this target. Missing/stale runtime files
 * cannot redirect the request or prevent removal of the extension's files. */
__declspec(dllexport) UINT __stdcall CheckRemovalRegistration(MSIHANDLE install)
{
    WCHAR *folder=calloc(32768,sizeof(WCHAR));DWORD size=32768;UINT result=ERROR_SUCCESS;
    if(!folder)return ERROR_INSTALL_FAILURE;
    if(MsiSetPropertyW(install,L"OaRemovalRegistrationOwned",L"")!=ERROR_SUCCESS){result=ERROR_INSTALL_FAILURE;goto done;}
    if(ValidateRecordedTarget(install)!=ERROR_SUCCESS){result=ERROR_INSTALL_FAILURE;goto done;}
    if(MsiGetPropertyW(install,L"HostFolder",folder,&size)!=ERROR_SUCCESS||!size){result=ERROR_INSTALL_FAILURE;goto done;}
    while(size>3&&folder[size-1]==L'\\')folder[--size]=0;
    if(registration_at(folder)&&MsiSetPropertyW(install,L"OaRemovalRegistrationOwned",L"1")!=ERROR_SUCCESS)result=ERROR_INSTALL_FAILURE;
done:free(folder);return result;
}

__declspec(dllexport) UINT __stdcall FindHostFolder(MSIHANDLE install)
{
    if(MsiSetPropertyW(install,L"OaHostFound",L"")!=ERROR_SUCCESS||MsiSetPropertyW(install,L"OaHostTaken",L"")!=ERROR_SUCCESS||MsiSetPropertyW(install,L"HostFolder",L"")!=ERROR_SUCCESS)return ERROR_INSTALL_FAILURE;
    COMFY_CONTEXT *context=calloc(1,sizeof(*context));COMFY_SELECTION *selection=calloc(1,sizeof(*selection));WCHAR *folder=NULL;UINT result=ERROR_INSTALL_FAILURE;
    if(context&&selection&&context_open(context,TRUE)&&current_selection(context,&folder,selection)){
        result=ERROR_SUCCESS;
        if(MsiSetPropertyW(install,L"HostFolder",folder)!=ERROR_SUCCESS||MsiSetPropertyW(install,L"OaHostFound",L"1")!=ERROR_SUCCESS||
           (foreign_folder(folder)&&MsiSetPropertyW(install,L"OaHostTaken",L"1")!=ERROR_SUCCESS))result=ERROR_INSTALL_FAILURE;
    }else{
        MSIHANDLE message=MsiCreateRecord(0);
        if(message){
            if(MsiRecordSetStringW(message,0,L"Choose a ready Comfy Desktop instance in the OpenAbstractions setup, then continue installation. The saved selection is missing, expired, or its host files have changed.")==ERROR_SUCCESS)
                MsiProcessMessage(install,INSTALLMESSAGE_ERROR,message);
            MsiCloseHandle(message);
        }
    }
    free(folder);free(selection);if(context){context_close(context);free(context);}return result;
}

/* Current-user selection only. No authority, installed ownership or Desktop
   configuration is supplied by the selection record. */
#ifndef OA_COMFY_PREPARE_SOURCE_INCLUDED
#define OA_COMFY_PREPARE_SOURCE_INCLUDED
#define UNICODE
#define _UNICODE
#include <windows.h>
#include <shlobj.h>
#include <tlhelp32.h>
#include <sddl.h>
#include <bcrypt.h>
#include <strsafe.h>
#include <wintrust.h>
#include <softpub.h>
#include <stdio.h>
#include <user_token.h>
#include "instance_reader.h"
#ifndef OA_COMFY_EXE_SHA256
#error The selected desktop binding header must be supplied by the owning builder.
#endif

#define SELECTION_LIMIT 8192
#define SELECTION_LIFETIME (30ULL*60*10000000)
static const WCHAR selection_name[]=L"comfyui-selection.json";
typedef struct {
    HANDLE token,parent;
    TOKEN_FACTS facts;
    WCHAR *roaming,*local,*directory,*selection,*desktop;
    DWORD parent_pid;
    ULONGLONG parent_created;
} COMFY_CONTEXT;
typedef struct {HANDLE handle;char *bytes;size_t size;FILE_ID_INFO identity;OA_NATIVE_FILE held;} COMFY_FILE;
static const char *selection_keys[]={"schema","nonce","created","expires","parent_pid","parent_created","sid","session","authentication","integrity","mode","instance_id","record_sha256","record_identity","host_sha256","host_identity"};
typedef struct {WCHAR fields[16][257];} COMFY_SELECTION;
static ULONGLONG file_time(FILETIME value){ULARGE_INTEGER n;n.LowPart=value.dwLowDateTime;n.HighPart=value.dwHighDateTime;return n.QuadPart;}
static ULONGLONG now_time(void){FILETIME value;GetSystemTimeAsFileTime(&value);return file_time(value);}
static void close_file(COMFY_FILE *file){oa_native_close(&file->held);free(file->bytes);ZeroMemory(file,sizeof(*file));}

/* Keep a stable ordinary file handle. No links or alternate streams are a
   supported source of either official records or the current selection. */
static int open_file(const WCHAR *path,size_t bound,BOOL retire,COMFY_FILE *out)
{
    ZeroMemory(out,sizeof(*out));
    if(!oa_native_open(path,GENERIC_READ|(retire?DELETE:0),FILE_SHARE_READ,FALSE,FALSE,&out->held))return 0;
    HANDLE handle=out->held.file;
    BY_HANDLE_FILE_INFORMATION info;LARGE_INTEGER size;DWORD got=0;int ok=0;
    OA_NATIVE_FILE_ID identity;
    if(!GetFileInformationByHandle(handle,&info)||info.nNumberOfLinks!=1||!GetFileSizeEx(handle,&size)||size.QuadPart<0||(ULONGLONG)size.QuadPart>bound)goto done;
    out->size=(size_t)size.QuadPart;out->bytes=calloc(out->size+1,1);
    if(!out->bytes||!ReadFile(handle,out->bytes,(DWORD)out->size,&got,NULL)||got!=out->size||!oa_native_id(handle,&identity))goto done;
    out->identity.VolumeSerialNumber=identity.volume;memcpy(out->identity.FileId.Identifier,identity.id,sizeof(identity.id));
    out->handle=handle;ok=1;
done:if(!ok)close_file(out);return ok;
}

static int digest_bytes(const void *bytes,size_t size,char output[65])
{
    BCRYPT_ALG_HANDLE algorithm=NULL;BCRYPT_HASH_HANDLE hash=NULL;DWORD length=0,got=0;BYTE *object=NULL,result[32];int ok=0;
    if(BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA256_ALGORITHM,NULL,0)<0||BCryptGetProperty(algorithm,BCRYPT_OBJECT_LENGTH,(BYTE*)&length,sizeof(length),&got,0)<0)goto done;
    object=malloc(length);if(!object||BCryptCreateHash(algorithm,&hash,object,length,NULL,0,0)<0||size>MAXDWORD||BCryptHashData(hash,(BYTE*)bytes,(ULONG)size,0)<0||BCryptFinishHash(hash,result,sizeof(result),0)<0)goto done;
    for(size_t i=0;i<32;i++)sprintf_s(output+i*2,3,"%02x",result[i]);ok=1;
done:if(hash)BCryptDestroyHash(hash);if(algorithm)BCryptCloseAlgorithmProvider(algorithm,0);free(object);return ok;
}
static void identity_text(const FILE_ID_INFO *identity,WCHAR output[49])
{
    const BYTE *bytes=(const BYTE*)identity;
    for(size_t i=0;i<sizeof(*identity);i++)swprintf_s(output+i*2,3,L"%02x",bytes[i]);
}
static int number_text(const WCHAR *text,ULONGLONG *number)
{
    if(!*text||wcslen(text)>20)return 0;ULONGLONG value=0;
    for(;*text;text++){if(*text<L'0'||*text>L'9'||value>(~0ULL-(ULONGLONG)(*text-L'0'))/10)return 0;value=value*10+(*text-L'0');}
    *number=value;return 1;
}
static int strict_selection(const COMFY_FILE *file,COMFY_SELECTION *out)
{
    scanner s={file->bytes,file->bytes+file->size};unsigned seen=0;
    blank(&s);if(s.p==s.end||*s.p++!='{')return 0;
    for(;;){blank(&s);
        char *decoded=malloc(file->size+1);if(!decoded)return 0;
        int valid=string(&s,decoded);int index=-1;
        if(valid)for(int i=0;i<16;i++)if(!strcmp(decoded,selection_keys[i]))index=i;
        free(decoded);if(!valid||index<0||(seen&(1u<<index)))return 0;seen|=1u<<index;
        blank(&s);if(s.p==s.end||*s.p++!=':')return 0;
        if(!oa_comfy_text(file->bytes,file->size,selection_keys[index],out->fields[index],257,TRUE))return 0;
        blank(&s);if(!string(&s,NULL))return 0;blank(&s);
        if(s.p==s.end)return 0;if(*s.p=='}'){s.p++;break;}if(*s.p++!=',')return 0;
    }
    blank(&s);return seen==0xffff&&s.p==s.end&&!wcscmp(out->fields[0],L"1");
}
static int hex_text(const WCHAR *text,size_t length)
{
    if(wcslen(text)!=length)return 0;
    for(;*text;text++)if(!((*text>=L'0'&&*text<=L'9')||(*text>=L'a'&&*text<=L'f')))return 0;
    return 1;
}
static int selection_shape(COMFY_SELECTION *selection)
{
    ULONGLONG made,expires,pid,parent;
    return hex_text(selection->fields[1],32)&&number_text(selection->fields[2],&made)&&made&&number_text(selection->fields[3],&expires)&&expires>=made&&expires-made==SELECTION_LIFETIME&&
        number_text(selection->fields[4],&pid)&&pid&&pid<=MAXDWORD&&number_text(selection->fields[5],&parent)&&parent&&
        (!wcscmp(selection->fields[10],L"standalone")||!wcscmp(selection->fields[10],L"adopted")||!wcscmp(selection->fields[10],L"legacy"))&&
        wcslen(selection->fields[11])<=128&&hex_text(selection->fields[12],64)&&hex_text(selection->fields[13],48)&&hex_text(selection->fields[14],64)&&hex_text(selection->fields[15],48);
}
static int token_selection(COMFY_CONTEXT *context,COMFY_SELECTION *selection)
{
    LPWSTR sid=NULL;WCHAR text[64];int ok=0;
    if(!ConvertSidToStringSidW(context->facts.sid,&sid)||wcscmp(sid,selection->fields[6]))goto done;
    swprintf_s(text,64,L"%lu",context->facts.session);if(wcscmp(text,selection->fields[7]))goto done;
    swprintf_s(text,64,L"%08lx%08lx",(DWORD)context->facts.authentication.HighPart,context->facts.authentication.LowPart);if(wcscmp(text,selection->fields[8]))goto done;
    swprintf_s(text,64,L"%lu",context->facts.integrity);if(wcscmp(text,selection->fields[9]))goto done;ok=1;
done:if(sid)LocalFree(sid);return ok;
}
/* -1 ambiguous/access denied, 0 exact old process exited/PID reused, 1 live. */
static int parent_state(DWORD pid,ULONGLONG creation)
{
    if(!pid||!creation)return -1;
    HANDLE process=OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION|SYNCHRONIZE,FALSE,pid);
    if(!process)return GetLastError()==ERROR_INVALID_PARAMETER?0:-1;
    FILETIME made,exit,kernel,user;int state=-1;
    if(GetProcessTimes(process,&made,&exit,&kernel,&user)){
        if(file_time(made)!=creation)state=0;
        else{DWORD wait=WaitForSingleObject(process,0);state=wait==WAIT_TIMEOUT?1:wait==WAIT_OBJECT_0?0:-1;}
    }
    CloseHandle(process);return state;
}
static void context_close(COMFY_CONTEXT *context)
{
    if(context->token)CloseHandle(context->token);if(context->parent)CloseHandle(context->parent);
    if(context->roaming)CoTaskMemFree(context->roaming);if(context->local)CoTaskMemFree(context->local);
    free(context->directory);free(context->selection);free(context->desktop);ZeroMemory(context,sizeof(*context));
}
static BOOL receiving_user(const TOKEN_FACTS *primary,const TOKEN_FACTS *user,BOOL receiving)
{
    return ordinary_user(user)&&(receiving||same_execution_user(primary,user));
}
static int context_open(COMFY_CONTEXT *context,BOOL receiving)
{
    ZeroMemory(context,sizeof(*context));TOKEN_FACTS primary={0};HANDLE token=NULL;int ok=0;
    context->token=user_token();
    /* user_token validates the actual thread impersonation first and permits
       process fallback only for an ordinary user. MSI must not query its
       service primary through that original-user impersonation. */
    if(!context->token||!token_facts(context->token,&context->facts)||(!receiving&&(!OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&token)||!token_facts(token,&primary)))||!receiving_user(&primary,&context->facts,receiving)||
       FAILED(SHGetKnownFolderPath(&FOLDERID_RoamingAppData,0,context->token,&context->roaming))||FAILED(SHGetKnownFolderPath(&FOLDERID_LocalAppData,0,context->token,&context->local)))goto done;
    context->directory=calloc(32768,sizeof(WCHAR));context->selection=calloc(32768,sizeof(WCHAR));context->desktop=calloc(32768,sizeof(WCHAR));
    if(!context->directory||!context->selection||!context->desktop||FAILED(StringCchPrintfW(context->directory,32768,L"%s\\OpenAbstractions\\Installer",context->local))||FAILED(StringCchPrintfW(context->selection,32768,L"%s\\%s",context->directory,selection_name)))goto done;
    /* Receiving MSI does not publish a selection or acquire a new lifetime.
       current_selection independently checks the recorded helper parent. */
    if(receiving){ok=1;goto done;}
    HANDLE snapshot=CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS,0);if(snapshot==INVALID_HANDLE_VALUE)goto done;
    PROCESSENTRY32W entry;entry.dwSize=sizeof(entry);
    if(Process32FirstW(snapshot,&entry))do{if(entry.th32ProcessID==GetCurrentProcessId()){context->parent_pid=entry.th32ParentProcessID;break;}}while(Process32NextW(snapshot,&entry));
    CloseHandle(snapshot);if(!context->parent_pid)goto done;
    context->parent=OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION|SYNCHRONIZE,FALSE,context->parent_pid);
    FILETIME made,exit,kernel,user;
    if(!context->parent||!GetProcessTimes(context->parent,&made,&exit,&kernel,&user)||WaitForSingleObject(context->parent,0)!=WAIT_TIMEOUT)goto done;
    context->parent_created=file_time(made);ok=1;
done:if(token)CloseHandle(token);if(!ok)context_close(context);return ok;
}

/* Exact installed host bytes come from the independently authenticated
   official-installer intake binding compiled into this own signed binary. */
static int desktop_check(COMFY_CONTEXT *context,FILE_ID_INFO *identity)
{
    HKEY user=NULL;WCHAR version[64];DWORD length=sizeof(version);int ok=0;
    if(RegOpenCurrentUser(KEY_READ,&user)!=ERROR_SUCCESS)return 0;
    if(RegGetValueW(user,OA_COMFY_ARP,L"DisplayVersion",RRF_RT_REG_SZ,NULL,version,&length)!=ERROR_SUCCESS||wcscmp(version,OA_COMFY_VERSION))goto done;
    length=32768*sizeof(WCHAR);
    /* This measured supplier record omits InstallLocation. Its DisplayIcon is
       an unquoted absolute executable path with the literal icon suffix ,0. */
    if(RegGetValueW(user,OA_COMFY_ARP,L"DisplayIcon",RRF_RT_REG_SZ,NULL,context->desktop,&length)!=ERROR_SUCCESS)goto done;
    size_t iconLength=wcslen(context->desktop);if(iconLength<3||wcscmp(context->desktop+iconLength-2,L",0"))goto done;
    context->desktop[iconLength-2]=0;if(!oa_comfy_plain(context->desktop,FALSE))goto done;
    const WCHAR *leaf=wcsrchr(context->desktop,L'\\');if(!leaf||wcscmp(leaf+1,OA_COMFY_EXE))goto done;
    OA_NATIVE_FILE held={0};if(!oa_native_open(context->desktop,GENERIC_READ,FILE_SHARE_READ,FALSE,FALSE,&held))goto done;
    HANDLE file=held.file;
    LARGE_INTEGER size;BY_HANDLE_FILE_INFORMATION info;BCRYPT_ALG_HANDLE algorithm=NULL;BCRYPT_HASH_HANDLE hash=NULL;BYTE *object=NULL,buffer[4096],result[32];DWORD objectSize=0,got=0;char hex[65];
    if(!GetFileSizeEx(file,&size)||(ULONGLONG)size.QuadPart!=OA_COMFY_EXE_BYTES||!GetFileInformationByHandle(file,&info)||info.nNumberOfLinks!=1||!GetFileInformationByHandleEx(file,FileIdInfo,identity,sizeof(*identity))||
       BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA256_ALGORITHM,NULL,0)<0||BCryptGetProperty(algorithm,BCRYPT_OBJECT_LENGTH,(BYTE*)&objectSize,sizeof(objectSize),&got,0)<0)goto file_done;
    object=malloc(objectSize);if(!object||BCryptCreateHash(algorithm,&hash,object,objectSize,NULL,0,0)<0)goto file_done;
    for(;;){if(!ReadFile(file,buffer,sizeof(buffer),&got,NULL))goto file_done;if(!got)break;if(BCryptHashData(hash,buffer,got,0)<0)goto file_done;}
    if(BCryptFinishHash(hash,result,sizeof(result),0)<0)goto file_done;
    for(size_t i=0;i<32;i++)sprintf_s(hex+i*2,3,"%02x",result[i]);
    if(strcmp(hex,OA_COMFY_EXE_SHA256))goto file_done;
    WINTRUST_FILE_INFO trusted={sizeof(trusted)};trusted.pcwszFilePath=context->desktop;trusted.hFile=file;
    WINTRUST_DATA trust={sizeof(trust)};trust.dwUIChoice=WTD_UI_NONE;trust.fdwRevocationChecks=WTD_REVOKE_NONE;trust.dwUnionChoice=WTD_CHOICE_FILE;trust.pFile=&trusted;trust.dwStateAction=WTD_STATEACTION_VERIFY;trust.dwProvFlags=WTD_CACHE_ONLY_URL_RETRIEVAL;
    GUID policy=WINTRUST_ACTION_GENERIC_VERIFY_V2;ok=WinVerifyTrust((HWND)INVALID_HANDLE_VALUE,&policy,&trust)==ERROR_SUCCESS;
    trust.dwStateAction=WTD_STATEACTION_CLOSE;WinVerifyTrust((HWND)INVALID_HANDLE_VALUE,&policy,&trust);
file_done:if(hash)BCryptDestroyHash(hash);if(algorithm)BCryptCloseAlgorithmProvider(algorithm,0);free(object);oa_native_close(&held);
done:RegCloseKey(user);return ok;
}

static void ascii_wide(const char *input,WCHAR *out){while(*input)*out++=(BYTE)*input++;*out=0;}
static int resolve_instance(COMFY_CONTEXT *context,const WCHAR *mode,const WCHAR *id,
                            OA_COMFY_INSTANCE *item,COMFY_FILE *record,WCHAR **folder)
{
    WCHAR *path=calloc(32768,sizeof(WCHAR));int ok=0;
    if(!path)return 0;
    BOOL legacy=!wcscmp(mode,L"legacy");
#if !OA_COMFY_ALLOW_LEGACY
    if(legacy||!wcscmp(mode,L"adopted"))goto done;
#endif
    if(!legacy&&wcscmp(mode,L"standalone")&&wcscmp(mode,L"adopted"))goto done;
    if(FAILED(StringCchPrintfW(path,32768,L"%s\\%s",context->roaming,legacy?L"ComfyUI\\config.json":OA_COMFY_RECORDS))||!open_file(path,MAX_CONFIG,FALSE,record))goto done;
    if(legacy){if(wcscmp(id,L"legacy")||!oa_comfy_legacy(record->bytes,record->size,item))goto done;}
    else if(oa_comfy_instance(record->bytes,record->size,id,item)!=1||item->adopted!=(BOOL)!wcscmp(mode,L"adopted"))goto done;
    if(!oa_comfy_paths(item,folder))goto done;
    ok=1;
done:free(path);if(!ok){close_file(record);free(*folder);*folder=NULL;}return ok;
}
static int foreign_folder(const WCHAR *folder)
{
    WCHAR *target=calloc(32768,sizeof(WCHAR)),*recorded=calloc(32768,sizeof(WCHAR));int foreign=1;HKEY user=NULL;
    if(!target||!recorded||FAILED(StringCchPrintfW(target,32768,L"%scomfyui-openabstractions",folder)))goto done;
    if(GetFileAttributesW(target)==INVALID_FILE_ATTRIBUTES){foreign=GetLastError()!=ERROR_FILE_NOT_FOUND&&GetLastError()!=ERROR_PATH_NOT_FOUND;goto done;}
    DWORD size=32768*sizeof(WCHAR);
    if(RegOpenCurrentUser(KEY_READ,&user)!=ERROR_SUCCESS||RegGetValueW(user,L"Software\\OpenAbstractions\\Extensions\\comfyui",L"folder",RRF_RT_REG_SZ,NULL,recorded,&size)!=ERROR_SUCCESS)goto done;
    foreign=!same_folder(folder,recorded,L"comfyui-openabstractions");
done:if(user)RegCloseKey(user);free(target);free(recorded);return foreign;
}
static int current_selection(COMFY_CONTEXT *context,WCHAR **folder,COMFY_SELECTION *selection)
{
    COMFY_FILE file={0},record={0};FILE_ID_INFO host;OA_COMFY_INSTANCE *item=calloc(1,sizeof(*item));int ok=0;
    ULONGLONG created,expires,pid,parent;WCHAR text[65];char digest[65];
    *folder=NULL;
    if(!item||!open_file(context->selection,SELECTION_LIMIT,FALSE,&file)||!strict_selection(&file,selection)||!selection_shape(selection)||!token_selection(context,selection)||
       !number_text(selection->fields[2],&created)||!number_text(selection->fields[3],&expires)||expires<created||expires-created!=SELECTION_LIFETIME||now_time()<created||now_time()>=expires||
       !number_text(selection->fields[4],&pid)||pid>MAXDWORD||!number_text(selection->fields[5],&parent)||parent_state((DWORD)pid,parent)!=1||
       wcslen(selection->fields[1])!=32||!desktop_check(context,&host))goto done;
    for(size_t i=0;i<32;i++)if(!((selection->fields[1][i]>=L'0'&&selection->fields[1][i]<=L'9')||(selection->fields[1][i]>=L'a'&&selection->fields[1][i]<=L'f')))goto done;
    ascii_wide(OA_COMFY_EXE_SHA256,text);if(wcscmp(text,selection->fields[14]))goto done;
    identity_text(&host,text);if(wcscmp(text,selection->fields[15]))goto done;
    if(!resolve_instance(context,selection->fields[10],selection->fields[11],item,&record,folder)||!digest_bytes(record.bytes,record.size,digest))goto done;
    ascii_wide(digest,text);if(wcscmp(text,selection->fields[12]))goto done;
    identity_text(&record.identity,text);if(wcscmp(text,selection->fields[13]))goto done;ok=1;
done:free(item);close_file(&record);close_file(&file);if(!ok){free(*folder);*folder=NULL;}return ok;
}

#ifndef OA_COMFY_ACTION_ONLY
/* Publish only after explicit selection. Existing recognized selection cannot
   be replaced while its exact OS parent is live, including after expiry. */
static int retire_stale(COMFY_CONTEXT *context)
{
    DWORD attributes=GetFileAttributesW(context->selection);
    if(attributes==INVALID_FILE_ATTRIBUTES)return GetLastError()==ERROR_FILE_NOT_FOUND||GetLastError()==ERROR_PATH_NOT_FOUND;
    COMFY_FILE file={0};COMFY_SELECTION *selection=calloc(1,sizeof(*selection));int ok=0;ULONGLONG pid,created;
    if(!selection||!open_file(context->selection,SELECTION_LIMIT,TRUE,&file)||!strict_selection(&file,selection)||!selection_shape(selection)||!token_selection(context,selection)||
       !number_text(selection->fields[4],&pid)||pid>MAXDWORD||!number_text(selection->fields[5],&created)||parent_state((DWORD)pid,created)!=0)goto done;
    FILE_DISPOSITION_INFO disposition={TRUE};ok=SetFileInformationByHandle(file.handle,FileDispositionInfo,&disposition,sizeof(disposition));
done:free(selection);close_file(&file);return ok;
}
static int ensure_directory(COMFY_CONTEXT *context,OA_NATIVE_FILE *held)
{
    if(!oa_native_open(context->local,FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE,TRUE,FALSE,held))return 0;
    HANDLE child=oa_native_at(held->file,L"OpenAbstractions",FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE,TRUE,3);
    if(child==INVALID_HANDLE_VALUE||!oa_native_add(held,child))goto failed;
    child=oa_native_at(held->file,L"Installer",FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE,TRUE,3);
    /* The selected directory name comes from context_open's fixed composition. */
    if(child==INVALID_HANDLE_VALUE||!oa_native_add(held,child))goto failed;
    {OA_NATIVE_FILE expected={0};int same=oa_native_open(context->directory,FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE,TRUE,FALSE,&expected)&&oa_native_same_handles(expected.file,held->file);oa_native_close(&expected);if(!same)goto failed;}
    return 1;
failed:oa_native_close(held);return 0;
}

static int escape_json(const WCHAR *value,char *output,size_t capacity)
{
    size_t used=0;
    for(;*value;value++){
        if(used+7>=capacity)return 0;
        if(*value>=0x20&&*value<=0x7e&&*value!=L'"'&&*value!=L'\\')output[used++]=(char)*value;
        else{sprintf_s(output+used,capacity-used,"\\u%04x",(unsigned)*value);used+=6;}
    }
    output[used]=0;return 1;
}
typedef struct {char digest[65];FILE_ID_INFO identity;} COMFY_SHOWN;
static int shown_record(const COMFY_FILE *record,COMFY_SHOWN *shown)
{shown->identity=record->identity;return digest_bytes(record->bytes,record->size,shown->digest);}
static int publish_selection(COMFY_CONTEXT *context,const WCHAR *mode,const WCHAR *id,const COMFY_SHOWN *shown)
{
    COMFY_SELECTION *selection=calloc(1,sizeof(*selection));OA_COMFY_INSTANCE *item=calloc(1,sizeof(*item));
    COMFY_FILE record={0};FILE_ID_INFO host;WCHAR *folder=NULL,*temporary=calloc(32768,sizeof(WCHAR));char *json=calloc(SELECTION_LIMIT,1),escaped[1600],digest[65];OA_NATIVE_FILE parent={0};HANDLE file=INVALID_HANDLE_VALUE;int ok=0;LPWSTR sid=NULL;BYTE nonce[16];
    if(!selection||!item||!temporary||!json||!desktop_check(context,&host)||!resolve_instance(context,mode,id,item,&record,&folder)||foreign_folder(folder)||
       !digest_bytes(record.bytes,record.size,digest)||!shown||strcmp(digest,shown->digest)||memcmp(&record.identity,&shown->identity,sizeof(record.identity))||!ConvertSidToStringSidW(context->facts.sid,&sid)||BCryptGenRandom(NULL,nonce,sizeof(nonce),BCRYPT_USE_SYSTEM_PREFERRED_RNG)<0||!ensure_directory(context,&parent)||!retire_stale(context))goto done;
    wcscpy_s(selection->fields[0],257,L"1");for(size_t i=0;i<16;i++)swprintf_s(selection->fields[1]+i*2,3,L"%02x",nonce[i]);
    ULONGLONG now=now_time();swprintf_s(selection->fields[2],257,L"%llu",now);swprintf_s(selection->fields[3],257,L"%llu",now+SELECTION_LIFETIME);
    swprintf_s(selection->fields[4],257,L"%lu",context->parent_pid);swprintf_s(selection->fields[5],257,L"%llu",context->parent_created);
    wcscpy_s(selection->fields[6],257,sid);swprintf_s(selection->fields[7],257,L"%lu",context->facts.session);
    swprintf_s(selection->fields[8],257,L"%08lx%08lx",(DWORD)context->facts.authentication.HighPart,context->facts.authentication.LowPart);swprintf_s(selection->fields[9],257,L"%lu",context->facts.integrity);
    wcscpy_s(selection->fields[10],257,mode);wcscpy_s(selection->fields[11],257,id);ascii_wide(digest,selection->fields[12]);identity_text(&record.identity,selection->fields[13]);ascii_wide(OA_COMFY_EXE_SHA256,selection->fields[14]);identity_text(&host,selection->fields[15]);
    size_t used=0;json[used++]='{';
    for(size_t i=0;i<16;i++){
        if(!escape_json(selection->fields[i],escaped,sizeof(escaped)))goto done;
        int count=sprintf_s(json+used,SELECTION_LIMIT-used,"%s\"%s\":\"%s\"",i?",":"",selection_keys[i],escaped);
        if(count<0)goto done;used+=(size_t)count;
    }
    if(used+2>=SELECTION_LIMIT)goto done;json[used++]='}';json[used++]='\n';
    if(FAILED(StringCchPrintfW(temporary,32768,L"%s\\comfyui-selection-%s.tmp",context->directory,selection->fields[1])))goto done;
    file=oa_native_at(parent.file,wcsrchr(temporary,L'\\')+1,GENERIC_WRITE|DELETE,0,FALSE,2);if(file==INVALID_HANDLE_VALUE)goto done;
    DWORD written=0;if(!WriteFile(file,json,(DWORD)used,&written,NULL)||written!=used||!FlushFileBuffers(file))goto done;
    int renamed=parent_state(context->parent_pid,context->parent_created)==1&&oa_native_rename_at(file,parent.file,selection_name);
    if(!renamed)goto done;
    temporary[0]=0;ok=1;
done:if(file!=INVALID_HANDLE_VALUE){if(!ok){FILE_DISPOSITION_INFO remove={TRUE};SetFileInformationByHandle(file,FileDispositionInfo,&remove,sizeof(remove));}CloseHandle(file);}oa_native_close(&parent);if(sid)LocalFree(sid);free(selection);free(item);free(folder);free(temporary);free(json);close_file(&record);return ok;
}

typedef struct {COMFY_CONTEXT *context;HWND dialog;WCHAR ids[256][129],modes[256][12];COMFY_SHOWN shown[256];size_t count;} COMFY_DIALOG;
static int list_record(const char *text,size_t size,void *opaque)
{
    COMFY_DIALOG *dialog=opaque;WCHAR id[129];
    if(!oa_comfy_text(text,size,"id",id,129,TRUE))return 0;
    OA_COMFY_INSTANCE *item=calloc(1,sizeof(*item));COMFY_FILE record={0};WCHAR *folder=NULL;int ok=1;
    if(!item)return 0;
    /* Resolve the ID from the complete authoritative file, never list order. */
    const WCHAR *modes[]={L"standalone",L"adopted"};
    for(size_t m=0;m<2;m++)if(resolve_instance(dialog->context,modes[m],id,item,&record,&folder)){
        if(dialog->count<256&&!foreign_folder(folder)&&shown_record(&record,&dialog->shown[dialog->count])){
            size_t at=dialog->count++;wcscpy_s(dialog->ids[at],129,id);wcscpy_s(dialog->modes[at],12,modes[m]);
            WCHAR *label=calloc(32768,sizeof(WCHAR));if(!label){ok=0;break;}
            if(SUCCEEDED(StringCchPrintfW(label,32768,L"%s — %s",item->name,item->base)))SendDlgItemMessageW(dialog->dialog,101,LB_ADDSTRING,0,(LPARAM)label);
            free(label);
        }
        break;
    }
    free(item);free(folder);close_file(&record);return ok;
}
static void refresh_dialog(COMFY_DIALOG *dialog)
{
    dialog->count=0;SendDlgItemMessageW(dialog->dialog,101,LB_RESETCONTENT,0,0);EnableWindow(GetDlgItem(dialog->dialog,IDOK),FALSE);
    WCHAR *path=calloc(32768,sizeof(WCHAR));COMFY_FILE file={0};
    if(path&&SUCCEEDED(StringCchPrintfW(path,32768,L"%s\\%s",dialog->context->roaming,OA_COMFY_RECORDS))&&open_file(path,MAX_CONFIG,FALSE,&file))
        if(!object_array(file.bytes,file.size,OA_COMFY_MAX_INSTANCES,list_record,dialog)){dialog->count=0;SendDlgItemMessageW(dialog->dialog,101,LB_RESETCONTENT,0,0);}
    close_file(&file);free(path);
#if OA_COMFY_ALLOW_LEGACY
    OA_COMFY_INSTANCE *legacy=calloc(1,sizeof(*legacy));COMFY_FILE record={0};WCHAR *folder=NULL;
    if(legacy&&dialog->count<256&&resolve_instance(dialog->context,L"legacy",L"legacy",legacy,&record,&folder)&&!foreign_folder(folder)&&shown_record(&record,&dialog->shown[dialog->count])){
        size_t at=dialog->count++;wcscpy_s(dialog->ids[at],129,L"legacy");wcscpy_s(dialog->modes[at],12,L"legacy");
        SendDlgItemMessageW(dialog->dialog,101,LB_ADDSTRING,0,(LPARAM)L"Legacy Desktop instance (explicit compatibility choice)");
    }
    free(legacy);free(folder);close_file(&record);
#endif
}
static INT_PTR CALLBACK prepare_dialog(HWND window,UINT message,WPARAM wparam,LPARAM lparam)
{
    COMFY_DIALOG *dialog=(COMFY_DIALOG*)GetWindowLongPtrW(window,GWLP_USERDATA);
    if(message==WM_INITDIALOG){dialog=(COMFY_DIALOG*)lparam;dialog->dialog=window;SetWindowLongPtrW(window,GWLP_USERDATA,(LONG_PTR)dialog);refresh_dialog(dialog);return TRUE;}
    if(message==WM_CLOSE){EndDialog(window,ERROR_INSTALL_USEREXIT);return TRUE;}
    if(message!=WM_COMMAND||!dialog)return FALSE;
    switch(LOWORD(wparam)){
    case IDCANCEL:EndDialog(window,ERROR_INSTALL_USEREXIT);return TRUE;
    case 103:refresh_dialog(dialog);return TRUE;
    case 101:if(HIWORD(wparam)==LBN_SELCHANGE)EnableWindow(GetDlgItem(window,IDOK),SendDlgItemMessageW(window,101,LB_GETCURSEL,0,0)!=LB_ERR);return TRUE;
    case 102:{FILE_ID_INFO identity;
        if(desktop_check(dialog->context,&identity)){
            WCHAR *command=calloc(32768,sizeof(WCHAR));STARTUPINFOW startup={sizeof(startup)};PROCESS_INFORMATION process;
            if(command&&SUCCEEDED(StringCchPrintfW(command,32768,L"\"%s\"",dialog->context->desktop))&&CreateProcessW(dialog->context->desktop,command,NULL,NULL,FALSE,0,NULL,NULL,&startup,&process)){CloseHandle(process.hThread);CloseHandle(process.hProcess);}
            else MessageBoxW(window,L"Desktop could not be opened. Existing files and settings were preserved.",L"ComfyUI preparation",MB_OK|MB_ICONERROR);
            free(command);
        }return TRUE;}
    case IDOK:{LRESULT selected=SendDlgItemMessageW(window,101,LB_GETCURSEL,0,0);
        if(selected>=0&&(size_t)selected<dialog->count&&publish_selection(dialog->context,dialog->modes[selected],dialog->ids[selected],&dialog->shown[selected]))EndDialog(window,0);
        else MessageBoxW(window,L"The instance or installer context changed, or another preparation is still active. Refresh and try again after that Setup has ended. No extension files were installed.",L"ComfyUI preparation",MB_OK|MB_ICONERROR);
        return TRUE;}
    }
    return FALSE;
}
int WINAPI wWinMain(HINSTANCE instance,HINSTANCE previous,LPWSTR arguments,int show)
{
    (void)previous;(void)show;BOOL guided=!wcscmp(arguments,L"--guided"),verify=!wcscmp(arguments,L"--verify-only");
    if(!guided&&!verify)return ERROR_INSTALL_FAILURE;
    COMFY_CONTEXT *context=calloc(1,sizeof(*context));COMFY_SELECTION *selection=calloc(1,sizeof(*selection));WCHAR *folder=NULL;FILE_ID_INFO host;int result=ERROR_INSTALL_FAILURE;
    if(!context||!selection||!context_open(context,FALSE)||!desktop_check(context,&host))goto done;
    if(verify){if(current_selection(context,&folder,selection)&&!foreign_folder(folder))result=0;goto done;}
    COMFY_DIALOG *dialog=calloc(1,sizeof(*dialog));if(!dialog)goto done;dialog->context=context;
    INT_PTR answer=DialogBoxParamW(instance,MAKEINTRESOURCEW(100),NULL,prepare_dialog,(LPARAM)dialog);free(dialog);
    result=answer==0?0:answer==ERROR_INSTALL_USEREXIT?ERROR_INSTALL_USEREXIT:ERROR_INSTALL_FAILURE;
done:free(folder);free(selection);if(context){context_close(context);free(context);}return result;
}
#endif
#endif /* OA_COMFY_PREPARE_SOURCE_INCLUDED */

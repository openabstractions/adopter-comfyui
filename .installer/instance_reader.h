/* Read-only Comfy Desktop production records. No configuration mutation or launch. */
#pragma once
#include "hostfolder_io.h"
#include <stdio.h>

/* Historical reader fixtures retain the supplier record location. The current
 * source-bound builder supplies its OA identity and disables foreign adoption. */
#ifndef OA_COMFY_RECORDS
#define OA_COMFY_RECORDS L"Comfy Desktop\\installations.json"
#endif
#ifndef OA_COMFY_ALLOW_LEGACY
#define OA_COMFY_ALLOW_LEGACY 1
#endif
#define OA_COMFY_MAX_INSTANCES 256
typedef struct {
    WCHAR id[129], name[257], base[32768], python[32768], source[32768];
    BOOL adopted, ready;
} OA_COMFY_INSTANCE;
typedef struct {
    const WCHAR *wanted;
    OA_COMFY_INSTANCE *selected;
    WCHAR ids[OA_COMFY_MAX_INSTANCES][129];
    size_t count;
    int found;
} OA_COMFY_SCAN;

/* JSON.parse's last-key semantics are inherited from the shared parser. */
static int oa_comfy_text(const char *record, size_t size, const char *key,
                         WCHAR *out, size_t capacity, BOOL required)
{
    char *utf8 = malloc(size + 1);
    if (!utf8) return 0;
    int state = top_level(record, size, key, utf8);
    WCHAR *decoded = state == 1 ? wide(utf8) : NULL;
    int ok = state >= 0 && (!required || (decoded && *decoded));
    if (state == 1 && (!decoded || wcslen(decoded) >= capacity)) ok = 0;
    if (ok) {
        out[0] = 0;
        if (decoded) wcscpy_s(out, capacity, decoded);
    }
    free(decoded); free(utf8);
    return ok;
}

/* Read a boolean with the same last-key rule; unknown fields stay opaque. */
static int oa_comfy_adopted(const char *text, size_t size, BOOL *adopted)
{
    scanner s = {text, text + size};
    char *key = malloc(size + 1);
    int ok = 0;
    *adopted = FALSE;
    if (!key) return 0;
    blank(&s);
    if (s.p == s.end || *s.p++ != '{') goto done;
    blank(&s);
    if (s.p < s.end && *s.p == '}') { ok = 1; goto done; }
    for (;;) {
        blank(&s);
        if (!string(&s, key)) goto done;
        blank(&s);
        if (s.p == s.end || *s.p++ != ':') goto done;
        blank(&s);
        if (!strcmp(key, "adopted")) {
            if (literal(&s, "true")) *adopted = TRUE;
            else if (literal(&s, "false")) *adopted = FALSE;
            else goto done;
        } else if (!value(&s, 1)) goto done;
        blank(&s);
        if (s.p == s.end) goto done;
        if (*s.p == '}') { ok = 1; break; }
        if (*s.p++ != ',') goto done;
    }
done: free(key); return ok;
}

static int oa_comfy_record(const char *record, size_t size, void *context)
{
    OA_COMFY_SCAN *scan = context;
    OA_COMFY_INSTANCE *item = calloc(1, sizeof(*item));
    WCHAR sourceId[129], status[129];
    WCHAR *install=calloc(32768,sizeof(WCHAR));
    int ok = 0;
    if (!item || !install) {free(item);free(install);return 0;}
    if (!oa_comfy_text(record,size,"id",item->id,129,TRUE) ||
        !oa_comfy_text(record,size,"name",item->name,257,TRUE) ||
        !oa_comfy_text(record,size,"sourceId",sourceId,129,TRUE)) goto done;
    for (size_t i=0;i<scan->count;i++) if (!wcscmp(scan->ids[i],item->id)) goto done;
    wcscpy_s(scan->ids[scan->count++],129,item->id);
    if (wcscmp(item->id,scan->wanted)) { ok=1; goto done; }
    scan->found=1;
    if (!oa_comfy_text(record,size,"status",status,129,FALSE) ||
        !oa_comfy_text(record,size,"installPath",install,32768,TRUE) ||
        !oa_comfy_adopted(record,size,&item->adopted)) goto done;
    item->ready = !wcscmp(sourceId,L"standalone") && !wcscmp(status,L"installed");
    if (item->adopted) {
        if (swprintf_s(item->source,32768,L"%s\\ComfyUI\\main.py",install)<0) goto done;
        if (!oa_comfy_text(record,size,"adoptedBaseDir",item->base,32768,TRUE) ||
            !oa_comfy_text(record,size,"adoptedPythonPath",item->python,32768,TRUE)) goto done;
    } else {
        if (swprintf_s(item->base,32768,L"%s\\backend\\ComfyUI",install)<0 ||
            swprintf_s(item->source,32768,L"%s\\main.py",item->base)<0 ||
            swprintf_s(item->python,32768,L"%s\\backend\\standalone-env\\python.exe",install)<0) goto done;
    }
    *scan->selected=*item; ok=1;
done: free(install);free(item); return ok;
}

/* -1 malformed/overbounds, 0 ID absent, 1 ready record, 2 not ready.
   This structural result never substitutes for oa_comfy_paths below. */
static int oa_comfy_instance(const char *text,size_t size,const WCHAR *id,OA_COMFY_INSTANCE *out)
{
    if (!text || !id || !*id || wcslen(id)>128 || !out) return -1;
    ZeroMemory(out,sizeof(*out));
    OA_COMFY_SCAN *scan=calloc(1,sizeof(*scan));
    if (!scan) return -1;
    scan->wanted=id; scan->selected=out;
    int valid=object_array(text,size,OA_COMFY_MAX_INSTANCES,oa_comfy_record,scan);
    int result=!valid?-1:!scan->found?0:out->ready?1:2;
    free(scan);
    if (result<1) ZeroMemory(out,sizeof(*out));
    return result;
}

/* roaming is supplied by the receiving token's known-folder owner, never by
   record JSON. This reader derives only the pinned production record path. */
static int oa_comfy_read(const WCHAR *roaming,const WCHAR *id,OA_COMFY_INSTANCE *out)
{
    if (out) ZeroMemory(out,sizeof(*out));
    WCHAR *path=calloc(32768,sizeof(WCHAR));
    if (!path || !roaming || !out) {free(path);return -1;}
    int result=-1;
    if (swprintf_s(path,32768,L"%s\\%s",roaming,OA_COMFY_RECORDS)<0) goto done;
    size_t size=0;
    char *text=read_config(path,&size);
    if (text) {result=oa_comfy_instance(text,size,id,out);free(text);}
done: free(path);return result;
}

/* Legacy is explicit: never inferred when a modern record is absent. */
static int oa_comfy_legacy(const char *text,size_t size,OA_COMFY_INSTANCE *out)
{
    if (!out) return 0;
    ZeroMemory(out,sizeof(*out));
    if (!text || size>MAX_CONFIG) return 0;
    if (!oa_comfy_text(text,size,"basePath",out->base,32768,TRUE)) return 0;
    if (swprintf_s(out->source,32768,L"%s\\main.py",out->base)<0 ||
        swprintf_s(out->python,32768,L"%s\\.venv\\Scripts\\python.exe",out->base)<0) return 0;
    wcscpy_s(out->id,129,L"legacy"); out->ready=TRUE; return 1;
}

/* Reject redirected ancestors and non-local input before the shared native
   host-folder resolver opens the receiving folder. */
static int oa_comfy_plain(const WCHAR *path,BOOL directory)
{
    OA_NATIVE_FILE held={0};
    int ok=oa_native_open(path,FILE_READ_ATTRIBUTES,FILE_SHARE_READ|FILE_SHARE_WRITE,directory,FALSE,&held);
    oa_native_close(&held);return ok;
}
static int oa_comfy_paths(const OA_COMFY_INSTANCE *item,WCHAR **folder)
{
    *folder=NULL;
    if (!item->ready || !oa_comfy_plain(item->base,TRUE) || !oa_comfy_plain(item->source,FALSE) ||
        !oa_comfy_plain(item->python,FALSE)) return 0;
    WCHAR *nodes=host_folder(item->base,L"custom_nodes");
    FILE_ID_INFO identity;
    if (!nodes || !oa_comfy_plain(nodes,TRUE) || !folder_id(nodes,&identity)) {free(nodes);return 0;}
    *folder=nodes; return 1;
}

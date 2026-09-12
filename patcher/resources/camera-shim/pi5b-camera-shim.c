/*
 * pi5b-camera-shim: make SystemCore's vision servers accept a USB camera
 * plugged straight into a Raspberry Pi 5 Model B.
 *
 * Each visionserverN canonicalises /sys/class/video4linux/videoN/device with
 * realpath(3) and compares the result against one hard-coded sysfs path that
 * includes the Limelight carrier board's internal USB hub:
 *
 *   .../1f00300000.usb/xhci-hcd.1/usb3/3-1/3-1.<port>/3-1.<port>:1.0
 *
 * A camera on a bare Pi 5B port has no hub level (".../usb3/3-1/3-1:1.0"),
 * so the comparison never matches and every server logs "no camera on this
 * port".  Loaded via LD_PRELOAD, this library wraps realpath() and rewrites
 * the canonical path of a camera that sits directly on a root port into the
 * hub form the binaries expect.  Nothing else is touched: the servers never
 * open anything under the canonical path (verified with strace), so no
 * reverse mapping is needed.
 *
 * Physical port -> virtual hub port (usb_id in the dashboard selects it):
 *   3-1 (USB 2.0, black, next to Ethernet) -> 3-1.1   usb_id 0 on any server
 *   3-2 (USB 2.0, black)                   -> 3-1.2   visionserver1, usb_id 1
 *   1-1 / 2-1 (USB 3.0, blue)              -> 3-1.3   visionserver2, usb_id 1
 *   1-2 / 2-2 (USB 3.0, blue)              -> 3-1.4   visionserver3, usb_id 1
 * Override with PI5B_CAMERA_PORTS="3-1=1,1-1=3,..." in the unit's environment.
 *
 * Cameras already behind a hub ("3-1.2/3-1.2:1.0") pass through unchanged.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stddef.h>

#define EXPECTED_PREFIX \
    "/sys/devices/platform/axi/1000120000.pcie/1f00300000.usb/xhci-hcd.1/usb3/3-1/"

static char *(*real_realpath)(const char *, char *);

/* Map "B-P" (e.g. "3-1") to a virtual hub port number, 0 = no mapping. */
static int virtual_port(const char *port)
{
    const char *env = getenv("PI5B_CAMERA_PORTS");
    const char *table = env && *env ? env : "3-1=1,3-2=2,1-1=3,2-1=3,1-2=4,2-2=4";
    size_t plen = strlen(port);
    const char *p = table;
    while (*p) {
        const char *eq = strchr(p, '=');
        const char *end = strchr(p, ',');
        if (!end) end = p + strlen(p);
        if (eq && eq < end && (size_t)(eq - p) == plen && strncmp(p, port, plen) == 0)
            return atoi(eq + 1);
        p = *end ? end + 1 : end;
    }
    return 0;
}

/*
 * If `path` ends in ".../usbB/B-P/B-P:C.I" (a device directly on a root
 * port) return 1 and write the hub-form path into `out` (PATH_MAX bytes).
 */
static int rewrite(const char *path, char *out)
{
    /* Split off the last three components. */
    const char *c3 = strrchr(path, '/');            /* /B-P:C.I */
    if (!c3 || c3 == path) return 0;
    const char *c2 = memrchr(path, '/', c3 - path); /* /B-P     */
    if (!c2 || c2 == path) return 0;
    const char *c1 = memrchr(path, '/', c2 - path); /* /usbB    */
    if (!c1) return 0;

    char bus[16], port[32], iface[32];
    size_t l1 = c2 - c1 - 1, l2 = c3 - c2 - 1, l3 = strlen(c3 + 1);
    if (l1 < 4 || l1 >= sizeof bus || l2 >= sizeof port || l3 >= sizeof iface) return 0;
    memcpy(bus, c1 + 1, l1); bus[l1] = 0;
    memcpy(port, c2 + 1, l2); port[l2] = 0;
    memcpy(iface, c3 + 1, l3); iface[l3] = 0;

    if (strncmp(bus, "usb", 3) != 0) return 0;
    /* port must be "<bus#>-<n>" with no '.', i.e. a root port, not a hub port */
    const char *dash = strchr(port, '-');
    if (!dash || strchr(port, '.') || strncmp(port, bus + 3, dash - port) != 0
        || (size_t)(dash - port) != strlen(bus + 3)) return 0;
    /* iface must be "<port>:<config>.<n>" */
    if (strncmp(iface, port, l2) != 0 || iface[l2] != ':') return 0;
    const char *suffix = iface + l2;                /* ":C.I" */
    if (!strchr(suffix, '.')) return 0;

    int vp = virtual_port(port);
    if (vp <= 0) return 0;

    int n = snprintf(out, PATH_MAX, EXPECTED_PREFIX "3-1.%d/3-1.%d%s", vp, vp, suffix);
    return n > 0 && n < PATH_MAX;
}

static char *shim_realpath(const char *path, char *resolved)
{
    char *res = real_realpath(path, resolved);
    if (!res || !strstr(res, "/usb")) return res;
    char fixed[PATH_MAX];
    if (rewrite(res, fixed)) {
        static int logged;
        if (!logged++)
            fprintf(stderr, "pi5b-camera-shim: presenting %s as %s\n", res, fixed);
        /* resolved is PATH_MAX bytes when supplied; glibc allocs PATH_MAX otherwise */
        strncpy(res, fixed, PATH_MAX - 1);
        res[PATH_MAX - 1] = 0;
    }
    return res;
}

char *realpath(const char *path, char *resolved)
{
    if (!real_realpath)
        real_realpath = dlsym(RTLD_NEXT, "realpath");
    return shim_realpath(path, resolved);
}

char *canonicalize_file_name(const char *path)
{
    if (!real_realpath)
        real_realpath = dlsym(RTLD_NEXT, "realpath");
    return shim_realpath(path, NULL);
}

/*
 * The vision servers are built with _FORTIFY_SOURCE, so their realpath()
 * calls are actually __realpath_chk(path, buf, buflen) — this is the entry
 * point that matters (verified with readelf --dyn-syms).  glibc requires
 * buflen >= PATH_MAX and our rewritten path is < PATH_MAX, so the in-place
 * rewrite above is safe here too.
 */
char *__realpath_chk(const char *path, char *resolved, size_t resolvedlen)
{
    static char *(*real_chk)(const char *, char *, size_t);
    if (!real_chk)
        real_chk = dlsym(RTLD_NEXT, "__realpath_chk");
    char *res = real_chk(path, resolved, resolvedlen);
    if (!res || !strstr(res, "/usb")) return res;
    char fixed[PATH_MAX];
    if (rewrite(res, fixed) && strlen(fixed) < resolvedlen) {
        static int logged;
        if (!logged++)
            fprintf(stderr, "pi5b-camera-shim: presenting %s as %s\n", res, fixed);
        strcpy(res, fixed);
    }
    return res;
}

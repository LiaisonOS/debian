/*
 * et-gps-sync — Bluetooth GPS Time Sync
 * Author: Sylvain Deguire (VA2OPS)
 * Date: April 2026
 * Updated: April 2026 — robust BT-buffer fix for TH-D75 and similar devices
 *
 * Reads NMEA sentences from /dev/rfcomm0, detects the true GPS second
 * boundary by watching the RMC timestamp INCREMENT, then calls
 * settimeofday() with sub-millisecond accuracy.
 *
 * Strategy:
 *   1. Read NMEA until first valid $GNRMC/$GPRMC — get position + date
 *   2. Keep reading RMC sentences, tracking the GPS second field.
 *      The moment the GPS second value changes (N -> N+1) we know we are
 *      standing exactly at a real second boundary regardless of how deep
 *      the device's Bluetooth buffer is.  Capture wall-clock at that instant.
 *   3. Compute delta = wall_now - gps_epoch of the NEW sentence.
 *      This delta is the sub-second phase error of our local clock.
 *   4. Set clock to gps_epoch + sub-second remainder only (no whole-second
 *      elapsed fudge — the GPS second field is authoritative).
 *   5. Print lat, lon, grid, time set to stdout for et-synching.py to parse
 *
 * Works correctly regardless of BT buffer depth (tested: TH-D74, TH-D75,
 * Samsung Android — all now agree within a few milliseconds).
 *
 * Compile:
 *   gcc -O2 -o et-gps-sync et-gps-sync.c -lm
 *
 * Usage:
 *   et-gps-sync [device]   (default: /dev/rfcomm0)
 *
 * Exit codes:
 *   0 = success
 *   1 = could not open device
 *   2 = timeout / no valid fix
 *   3 = settimeofday failed (need root)
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <termios.h>
#include <time.h>
#include <sys/time.h>
#include <math.h>
#include <errno.h>

#define TIMEOUT_SEC     60
#define MAX_LINE        256
#define DEFAULT_DEV     "/dev/rfcomm0"

/* -------------------------------------------------------------------------
 * Serial port setup
 * ---------------------------------------------------------------------- */
static int open_serial(const char *dev) {
    int fd = open(dev, O_RDONLY | O_NOCTTY);
    if (fd < 0) return -1;
    struct termios tty;
    tcgetattr(fd, &tty);
    cfsetispeed(&tty, B9600);
    cfsetospeed(&tty, B9600);
    tty.c_cflag = (tty.c_cflag & ~CSIZE) | CS8;
    tty.c_cflag |= CLOCAL | CREAD;
    tty.c_cflag &= ~(PARENB | PARODD | CSTOPB | CRTSCTS);
    tty.c_iflag = IGNBRK | IGNPAR;
    tty.c_oflag = 0;
    tty.c_lflag = 0;
    tty.c_cc[VMIN]  = 1;
    tty.c_cc[VTIME] = 0;
    tcsetattr(fd, TCSANOW, &tty);
    tcflush(fd, TCIFLUSH);   /* discard any buffered input from before open */
    return fd;
}

/* -------------------------------------------------------------------------
 * Read one '\n'-terminated line from fd into buf (max len-1 chars)
 * Returns number of chars read, or -1 on error/timeout
 * ---------------------------------------------------------------------- */
static int read_line(int fd, char *buf, int len) {
    int i = 0;
    char c;
    while (i < len - 1) {
        int n = read(fd, &c, 1);
        if (n <= 0) return -1;
        if (c == '\n') break;
        if (c != '\r') buf[i++] = c;
    }
    buf[i] = '\0';
    return i;
}

/* -------------------------------------------------------------------------
 * NMEA field extraction: split sentence by comma, return Nth field
 * ---------------------------------------------------------------------- */
static void nmea_field(const char *sentence, int n, char *out, int outlen) {
    const char *p = sentence;
    int field = 0;
    out[0] = '\0';
    while (*p && field < n) {
        if (*p++ == ',') field++;
    }
    int i = 0;
    while (*p && *p != ',' && *p != '*' && i < outlen - 1)
        out[i++] = *p++;
    out[i] = '\0';
}

/* -------------------------------------------------------------------------
 * Parse $GPRMC / $GNRMC sentence
 * Returns 1 on success, 0 on invalid/no-fix
 * Fields: time(1), status(2), lat(3), NS(4), lon(5), EW(6), date(9)
 * ---------------------------------------------------------------------- */
typedef struct {
    double lat, lon;
    int    year, mon, day, hour, min, sec;
    int    valid;
} RMC;

static int parse_rmc(const char *line, RMC *r) {
    char f[32];

    /* Status must be 'A' (active) */
    nmea_field(line, 2, f, sizeof(f));
    if (f[0] != 'A') return 0;

    /* Time: HHMMSS.ss */
    nmea_field(line, 1, f, sizeof(f));
    if (strlen(f) < 6) return 0;
    r->hour = (f[0]-'0')*10 + (f[1]-'0');
    r->min  = (f[2]-'0')*10 + (f[3]-'0');
    r->sec  = (f[4]-'0')*10 + (f[5]-'0');

    /* Date: DDMMYY */
    nmea_field(line, 9, f, sizeof(f));
    if (strlen(f) < 6) return 0;
    r->day  = (f[0]-'0')*10 + (f[1]-'0');
    r->mon  = (f[2]-'0')*10 + (f[3]-'0');
    r->year = 2000 + (f[4]-'0')*10 + (f[5]-'0');

    /* Latitude */
    nmea_field(line, 3, f, sizeof(f));
    if (!f[0]) return 0;
    double raw_lat = atof(f);
    int lat_deg = (int)(raw_lat / 100);
    r->lat = lat_deg + (raw_lat - lat_deg * 100) / 60.0;
    nmea_field(line, 4, f, sizeof(f));
    if (f[0] == 'S') r->lat = -r->lat;

    /* Longitude */
    nmea_field(line, 5, f, sizeof(f));
    if (!f[0]) return 0;
    double raw_lon = atof(f);
    int lon_deg = (int)(raw_lon / 100);
    r->lon = lon_deg + (raw_lon - lon_deg * 100) / 60.0;
    nmea_field(line, 6, f, sizeof(f));
    if (f[0] == 'W') r->lon = -r->lon;

    r->valid = 1;
    return 1;
}

/* -------------------------------------------------------------------------
 * Convert RMC date/time to time_t (UTC)
 * ---------------------------------------------------------------------- */
static time_t rmc_to_epoch(const RMC *r) {
    struct tm t = {0};
    t.tm_year = r->year - 1900;
    t.tm_mon  = r->mon - 1;
    t.tm_mday = r->day;
    t.tm_hour = r->hour;
    t.tm_min  = r->min;
    t.tm_sec  = r->sec;
    t.tm_isdst = 0;
    return timegm(&t);
}

/* -------------------------------------------------------------------------
 * Maidenhead grid from lat/lon (6-char)
 * ---------------------------------------------------------------------- */
static void latlon_to_grid(double lat, double lon, char *grid) {
    lon += 180.0; lat += 90.0;
    int A = (int)(lon / 20);
    int B = (int)(lat / 10);
    int C = (int)((lon - A*20) / 2);
    int D = (int)(lat - B*10);
    int E = (int)((lon - A*20 - C*2) * 12);
    int F = (int)((lat - B*10 - D) * 24);
    grid[0] = 'A' + A;
    grid[1] = 'A' + B;
    grid[2] = '0' + C;
    grid[3] = '0' + D;
    grid[4] = 'a' + E;
    grid[5] = 'a' + F;
    grid[6] = '\0';
}

/* -------------------------------------------------------------------------
 * Main
 * ---------------------------------------------------------------------- */
int main(int argc, char *argv[]) {
    const char *dev = (argc > 1) ? argv[1] : DEFAULT_DEV;
    long offset_sec  = (argc > 2) ? atol(argv[2]) : 0;

    /* Special device "-" means read NMEA from stdin (e.g. piped from gpspipe) */
    int fd;
    if (strcmp(dev, "-") == 0) {
        fd = STDIN_FILENO;
    } else {
        fd = open_serial(dev);
        if (fd < 0) {
            fprintf(stderr, "ERROR: Cannot open %s: %s\n", dev, strerror(errno));
            return 1;
        }
    }

    char line[MAX_LINE];
    RMC first = {0};
    time_t deadline = time(NULL) + TIMEOUT_SEC;

    /* ---- Phase 1: find first valid RMC (position + date) ---- */
    fprintf(stdout, "STATUS: Waiting for GPS fix...\n");
    fflush(stdout);

    while (time(NULL) < deadline) {
        if (read_line(fd, line, sizeof(line)) < 0) {
            fprintf(stderr, "ERROR: Read error on %s\n", dev);
            if (fd != STDIN_FILENO) close(fd);
            return 2;
        }
        if (strncmp(line, "$GPRMC", 6) == 0 || strncmp(line, "$GNRMC", 6) == 0) {
            if (parse_rmc(line, &first)) {
                char grid[8];
                latlon_to_grid(first.lat, first.lon, grid);
                fprintf(stdout, "FIX: %.6f %.6f %s\n", first.lat, first.lon, grid);
                fflush(stdout);
                break;
            }
        }
    }

    if (!first.valid) {
        fprintf(stderr, "ERROR: Timeout — no valid GPS fix\n");
        if (fd != STDIN_FILENO) close(fd);
        return 2;
    }

    /* ---- Phase 2: detect true second boundary by watching GPS sec increment ----
     *
     * We read RMC sentences continuously, comparing each sentence's GPS second
     * to the previous one.  The instant the second field increments (or wraps
     * across a minute boundary) we know a real GPS second boundary just passed,
     * regardless of how many sentences the device's BT stack had buffered.
     *
     * At that exact moment we capture wall-clock time.  The sub-second part of
     * that wall-clock reading tells us the phase offset to apply.
     * -------------------------------------------------------------------- */
    fprintf(stdout, "STATUS: Waiting for second boundary...\n");
    fflush(stdout);

    RMC  prev    = first;           /* seed with the fix we already have    */
    RMC  current = {0};
    struct timespec boundary_ts = {0};
    int  found   = 0;

    while (time(NULL) < deadline) {
        if (read_line(fd, line, sizeof(line)) < 0) break;

        if (strncmp(line, "$GPRMC", 6) != 0 && strncmp(line, "$GNRMC", 6) != 0)
            continue;

        if (!parse_rmc(line, &current))
            continue;

        /* Compute GPS epoch for both sentences */
        time_t t_prev    = rmc_to_epoch(&prev);
        time_t t_current = rmc_to_epoch(&current);

        if (t_current != t_prev) {
            /* The GPS second just ticked — capture wall clock immediately */
            clock_gettime(CLOCK_REALTIME, &boundary_ts);

            long lag = (long)boundary_ts.tv_sec - (long)t_current;

            fprintf(stdout, "DEBUG: prev_epoch=%ld current_epoch=%ld "
                            "bt_buffer_depth=%lds wall=%ld.%06ld lag=%lds\n",
                    (long)t_prev, (long)t_current,
                    (long)(t_current - t_prev - 1),   /* sentences buffered   */
                    boundary_ts.tv_sec,
                    boundary_ts.tv_nsec / 1000L,
                    lag);
            fflush(stdout);

            if (lag <= 1) {
                /* GPS epoch matches wall clock — BT buffer drained */
                found = 1;
                break;
            }

            /* Still reading from BT buffer — keep scanning for a later transition */
            fprintf(stdout, "STATUS: BT buffer draining (%lds behind), continuing...\n", lag);
            fflush(stdout);
        }

        prev = current;   /* same second (or still buffered) — keep scanning */
    }

    if (!found || !current.valid) {
        fprintf(stderr, "ERROR: Timeout waiting for second boundary\n");
        if (fd != STDIN_FILENO) close(fd);
        return 2;
    }

    /* ---- Phase 3: set clock to GPS epoch + sub-second wall remainder ----
     *
     * boundary_ts.tv_sec  is the wall second when we caught the transition.
     * boundary_ts.tv_nsec is the sub-second phase offset of our local clock
     * relative to the true GPS second boundary.
     *
     * The correct UTC time is:  gps_epoch + sub-second phase
     * We do NOT add any whole-second elapsed — the GPS second field is
     * authoritative and already accounts for any BT buffer depth.
     * -------------------------------------------------------------------- */
    time_t gps_epoch  = rmc_to_epoch(&current) + offset_sec;
    long   subsec_us  = (long)(boundary_ts.tv_nsec / 1000L);

    /* Paranoia: sub-second must be in [0, 999999] */
    if (subsec_us < 0 || subsec_us >= 1000000L)
        subsec_us = 0;

    if (offset_sec != 0)
        fprintf(stdout, "STATUS: Applying GPS offset: %+lds\n", offset_sec);

    struct timeval tv;
    tv.tv_sec  = gps_epoch;
    tv.tv_usec = subsec_us;

    if (settimeofday(&tv, NULL) != 0) {
        fprintf(stderr, "ERROR: settimeofday failed: %s (need root)\n", strerror(errno));
        return 3;
    }

    /* Sync hardware clock (return value intentionally ignored) */
    chdir("/tmp");
    if (system("hwclock --systohc 2>/dev/null")) { /* suppress warn_unused_result */ }

    fprintf(stdout, "TIME: %04d-%02d-%02d %02d:%02d:%02d UTC +%ldus phase\n",
            current.year, current.mon, current.day,
            current.hour, current.min, current.sec,
            subsec_us);
    fflush(stdout);

    /* ---- Phase 4: verification — read next RMC and compare ----------------
     *
     * Wait for the next GPS second boundary, then check if our newly-set
     * system clock agrees.  If there is still a whole-second delta, apply
     * a correction and report it.
     * ----------------------------------------------------------------------- */
    fprintf(stdout, "STATUS: Verifying clock accuracy...\n");
    fflush(stdout);

    {
        time_t   verify_deadline = time(NULL) + 10;
        RMC      vprev    = current;
        RMC      vcurrent = {0};
        int      vfound   = 0;

        while (time(NULL) < verify_deadline) {
            if (read_line(fd, line, sizeof(line)) < 0) break;
            if (strncmp(line, "$GPRMC", 6) != 0 && strncmp(line, "$GNRMC", 6) != 0)
                continue;
            if (!parse_rmc(line, &vcurrent))
                continue;

            time_t vt_prev    = rmc_to_epoch(&vprev);
            time_t vt_current = rmc_to_epoch(&vcurrent);

            if (vt_current != vt_prev) {
                struct timespec vwall;
                clock_gettime(CLOCK_REALTIME, &vwall);
                long delta = (long)vwall.tv_sec - (long)vt_current;

                fprintf(stdout, "STATUS: Verification — GPS=%ld wall=%ld delta=%+lds\n",
                        (long)vt_current, vwall.tv_sec, delta);
                fflush(stdout);

                if (delta != 0) {
                    /* Apply correction */
                    struct timeval tvcorr;
                    tvcorr.tv_sec  = vt_current;
                    tvcorr.tv_usec = (long)(vwall.tv_nsec / 1000L);
                    if (settimeofday(&tvcorr, NULL) == 0) {
                        fprintf(stdout, "STATUS: Correction applied (%+lds)\n", delta);
                        if (system("hwclock --systohc 2>/dev/null")) { /* suppress */ }
                    }
                } else {
                    fprintf(stdout, "STATUS: Clock verified OK (0s delta)\n");
                }
                fflush(stdout);
                vfound = 1;
                break;
            }
            vprev = vcurrent;
        }

        if (!vfound)
            fprintf(stdout, "STATUS: Verification timeout (clock set, not verified)\n");
        fflush(stdout);
    }

    if (fd != STDIN_FILENO) close(fd);

    return 0;
}

// Validation rules for values typed into the captive portal, before they are
// persisted to NVS over a known-good stored config.
//
// Deliberately free of Arduino/IDF types (plain C strings only) so the rules are
// unit-testable on a host — see `firmware/test/test_portal_fields.cpp`. The firmware
// keeps its config in Arduino `String` globals; the thin glue that copies validated
// values into them lives in main.cpp / gateway.ino.
//
// The guiding rule: a portal submit must never be able to leave the gateway in a
// worse state than before. A field the user cleared, mistyped, or that would corrupt
// an MQTT topic is REJECTED and the stored value is kept, rather than written through.
//
// This file exists TWICE, byte-identical, because arduino-cli only compiles headers
// that live inside the sketch directory:
//     firmware/gateway-idf/main/portal_fields.h   (ESP-IDF build)
//     firmware/gateway/portal_fields.h            (arduino-cli build)
// Edit one, copy to the other — `firmware/test/run.sh` fails if they drift apart.
#pragma once

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// Copy `in` into `out` (capacity `cap`) with leading/trailing whitespace removed.
// Always NUL-terminates; truncates rather than overflowing. Returns trimmed length.
// Phone keyboards love to append a space, so trimming is what makes "192.168.1.5 "
// and "192.168.1.5" the same edit instead of a spurious change.
inline size_t pf_trim(const char* in, char* out, size_t cap) {
  if (!out || cap == 0) return 0;
  out[0] = '\0';
  if (!in) return 0;
  const char* b = in;
  while (*b && isspace((unsigned char)*b)) b++;
  const char* e = b + strlen(b);
  while (e > b && isspace((unsigned char)e[-1])) e--;
  size_t n = (size_t)(e - b);
  if (n > cap - 1) n = cap - 1;
  memcpy(out, b, n);
  out[n] = '\0';
  return n;
}

// A broker host is usable if it is non-empty after trimming. An empty field means the
// user cleared it (or the browser submitted a blank); persisting that would leave the
// gateway with nowhere to publish, so we keep whatever is already stored.
inline bool pf_valid_host(const char* trimmed) {
  return trimmed && trimmed[0] != '\0';
}

// A port must be all digits and land in 1..65535. Blank, 0, "abc" and 70000 all leave
// the stored port untouched.
inline bool pf_parse_port(const char* trimmed, int* out) {
  if (!trimmed || trimmed[0] == '\0') return false;
  for (const char* p = trimmed; *p; ++p) {
    if (!isdigit((unsigned char)*p)) return false;
  }
  long v = strtol(trimmed, NULL, 10);
  if (v < 1 || v > 65535) return false;
  if (out) *out = (int)v;
  return true;
}

// The gateway id is interpolated straight into MQTT topics ("cuddle/<gw>/hr/<dev>"),
// so it must not contain a topic separator ('/') or the wildcards ('+', '#') — those
// would silently reshape the topic tree and make the gateway subscribe to, or publish
// on, paths the app never reads. Whitespace/control chars and quote/backslash are also
// rejected so the id stays safe inside the JSON report payload and serial logs.
inline bool pf_valid_gwid(const char* trimmed) {
  if (!trimmed || trimmed[0] == '\0') return false;
  for (const char* p = trimmed; *p; ++p) {
    const char c = *p;
    if (c == '/' || c == '+' || c == '#' || c == '"' || c == '\\') return false;
    if ((unsigned char)c < 0x20 || isspace((unsigned char)c)) return false;
  }
  return true;
}

// The gateway's persisted settings, as plain storage so the whole decision below can
// run (and be tested) without Arduino types.
struct PfConfig {
  char broker[48];
  int  port;
  char gwid[32];
};

// Fold one portal submit into `cfg`, field by field: a value is written only if it
// passes its rule AND differs from what is already stored. Returns true if `cfg`
// changed — the caller uses that to decide whether to touch NVS at all, so an
// untouched submit (the portal pre-fills every field with the current value) writes
// nothing, and an invalid field leaves the good stored value in place.
inline bool pf_apply(PfConfig* cfg, const char* broker, const char* port, const char* gwid) {
  if (!cfg) return false;
  bool changed = false;

  // Trim straight into a destination-sized buffer, so an over-long field is bounded
  // here rather than truncated on the way into the config.
  char host[sizeof(cfg->broker)];
  pf_trim(broker, host, sizeof(host));
  if (pf_valid_host(host) && strcmp(cfg->broker, host) != 0) {
    memcpy(cfg->broker, host, sizeof(host));
    changed = true;
  }

  char portbuf[16];
  int p = 0;
  pf_trim(port, portbuf, sizeof(portbuf));
  if (pf_parse_port(portbuf, &p) && p != cfg->port) {
    cfg->port = p;
    changed = true;
  }

  char id[sizeof(cfg->gwid)];
  pf_trim(gwid, id, sizeof(id));
  if (pf_valid_gwid(id) && strcmp(cfg->gwid, id) != 0) {
    memcpy(cfg->gwid, id, sizeof(id));
    changed = true;
  }

  return changed;
}

// Host tests for the captive-portal field rules that gate what reaches NVS.
//
// These compile the REAL firmware header (no ESP toolchain needed), so the decisions
// exercised here are the ones the gateway actually makes when someone hits Save in the
// portal. Build + run with `firmware/test/run.sh`.

#include "../gateway-idf/main/portal_fields.h"

#include <stdio.h>
#include <string.h>

static int failures = 0;

#define CHECK(cond, msg)                                                       \
  do {                                                                         \
    if (!(cond)) {                                                             \
      printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, (msg));                 \
      failures++;                                                              \
    }                                                                          \
  } while (0)

static PfConfig baseline() {
  PfConfig c;
  snprintf(c.broker, sizeof(c.broker), "%s", "192.168.1.212");
  snprintf(c.gwid, sizeof(c.gwid), "%s", "esp32-01-a172e0");
  c.port = 1883;
  return c;
}

static void test_trim() {
  char buf[16];
  pf_trim("  hi  ", buf, sizeof(buf));
  CHECK(strcmp(buf, "hi") == 0, "trims both ends");

  pf_trim("\t\n x \r\n", buf, sizeof(buf));
  CHECK(strcmp(buf, "x") == 0, "trims tabs/newlines");

  pf_trim("   ", buf, sizeof(buf));
  CHECK(buf[0] == '\0', "all-whitespace becomes empty");

  pf_trim(NULL, buf, sizeof(buf));
  CHECK(buf[0] == '\0', "NULL input becomes empty");

  char small[4];
  pf_trim("abcdefgh", small, sizeof(small));
  CHECK(strcmp(small, "abc") == 0, "truncates instead of overflowing");
}

static void test_port_rules() {
  int p = 0;
  CHECK(pf_parse_port("1883", &p) && p == 1883, "accepts a normal port");
  CHECK(pf_parse_port("1", &p) && p == 1, "accepts the low bound");
  CHECK(pf_parse_port("65535", &p) && p == 65535, "accepts the high bound");
  CHECK(!pf_parse_port("0", &p), "rejects 0");
  CHECK(!pf_parse_port("65536", &p), "rejects above the high bound");
  CHECK(!pf_parse_port("", &p), "rejects blank");
  CHECK(!pf_parse_port("18a3", &p), "rejects non-digits");
  CHECK(!pf_parse_port("-5", &p), "rejects negative");
  CHECK(!pf_parse_port("1883 ", &p), "rejects untrimmed input (pf_apply trims first)");
}

static void test_gwid_rules() {
  CHECK(pf_valid_gwid("esp32-01-a172e0"), "accepts a normal id");
  CHECK(!pf_valid_gwid(""), "rejects empty");
  // These would silently reshape "cuddle/<gw>/hr/<dev>".
  CHECK(!pf_valid_gwid("esp32/01"), "rejects topic separator '/'");
  CHECK(!pf_valid_gwid("esp32+01"), "rejects MQTT wildcard '+'");
  CHECK(!pf_valid_gwid("esp32#01"), "rejects MQTT wildcard '#'");
  CHECK(!pf_valid_gwid("esp32 01"), "rejects embedded whitespace");
  CHECK(!pf_valid_gwid("esp\"32"), "rejects quote (breaks the JSON report)");
  CHECK(!pf_valid_gwid("esp\\32"), "rejects backslash");
}

static void test_apply_persists_real_edits() {
  PfConfig c = baseline();
  CHECK(pf_apply(&c, "10.0.0.5", "1884", "gw-lounge"), "reports a change");
  CHECK(strcmp(c.broker, "10.0.0.5") == 0, "broker updated");
  CHECK(c.port == 1884, "port updated");
  CHECK(strcmp(c.gwid, "gw-lounge") == 0, "gwid updated");
}

static void test_apply_is_noop_when_unchanged() {
  // The portal pre-fills every field with the current value, so an untouched submit
  // must not report a change (that is what keeps us from rewriting NVS every boot).
  PfConfig c = baseline();
  CHECK(!pf_apply(&c, "192.168.1.212", "1883", "esp32-01-a172e0"), "no change reported");

  // ...and the same values with phone-keyboard whitespace are still not a change.
  PfConfig c2 = baseline();
  CHECK(!pf_apply(&c2, " 192.168.1.212 ", " 1883 ", " esp32-01-a172e0 "),
        "whitespace-only difference is not a change");
}

static void test_apply_never_clobbers_with_bad_input() {
  // The core safety property: a bad or cleared field leaves the stored value alone,
  // so a fat-fingered submit can't take the gateway off the air.
  PfConfig c = baseline();
  CHECK(!pf_apply(&c, "", "", ""), "all-empty submit changes nothing");
  CHECK(strcmp(c.broker, "192.168.1.212") == 0, "broker kept");
  CHECK(c.port == 1883, "port kept");
  CHECK(strcmp(c.gwid, "esp32-01-a172e0") == 0, "gwid kept");

  PfConfig c2 = baseline();
  CHECK(!pf_apply(&c2, "   ", "abc", "bad/id"), "invalid submit changes nothing");
  CHECK(strcmp(c2.broker, "192.168.1.212") == 0, "broker kept vs whitespace");
  CHECK(c2.port == 1883, "port kept vs non-numeric");
  CHECK(strcmp(c2.gwid, "esp32-01-a172e0") == 0, "gwid kept vs topic-unsafe");
}

static void test_apply_partial_edit() {
  // One good field + two bad ones: the good one lands, the bad ones are ignored.
  PfConfig c = baseline();
  CHECK(pf_apply(&c, "10.0.0.9", "70000", ""), "reports the one real change");
  CHECK(strcmp(c.broker, "10.0.0.9") == 0, "valid broker applied");
  CHECK(c.port == 1883, "out-of-range port ignored");
  CHECK(strcmp(c.gwid, "esp32-01-a172e0") == 0, "empty gwid ignored");
}

int main() {
  printf("portal_fields host tests\n");
  test_trim();
  test_port_rules();
  test_gwid_rules();
  test_apply_persists_real_edits();
  test_apply_is_noop_when_unchanged();
  test_apply_never_clobbers_with_bad_input();
  test_apply_partial_edit();

  if (failures) {
    printf("%d FAILED\n", failures);
    return 1;
  }
  printf("all passed\n");
  return 0;
}

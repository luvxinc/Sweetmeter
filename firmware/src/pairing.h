#pragma once
// Paired computers and the per-connection challenge-response hello.
// A host ID is only a routing label; a link is authorized solely by proving
// possession of the 32-byte secret that computer registered while the owner
// had the physical selection menu open. The meter remembers up to eight paired
// computers; exactly one (the selected one) may drive the dashboard.
#ifdef SWEETMETER_NATIVE_TEST
#include "firmware_stubs.h"
#else
#include <mbedtls/md.h>
#endif
#include "ota_protocol.h"

uint32_t crc32(const uint8_t *bytes, size_t size);

namespace sweetmeter {
constexpr size_t secretSize = 32, challengeSize = 16, proofSize = 16, serialSize = 12, maxPaired = 8;
// Hello ACK results (`H:u8, result:u8`, sent for H and P); Y replies use 0/5/7.
constexpr uint8_t helloOk = 0, helloStoreFailed = 5, helloRejected = 7, helloProvision = 8, helloNotSelected = 9;
constexpr char authLabel[] = "SWM-AUTH-1";

inline bool constantTimeEqual(const uint8_t *a, const uint8_t *b, size_t n) {
  volatile uint8_t difference = 0;
  for (size_t i = 0; i < n; ++i) difference = difference | uint8_t(a[i] ^ b[i]);
  return difference == 0;
}
inline bool usableSecret(const uint8_t *secret) {
  uint8_t any = 0;
  for (size_t i = 0; i < secretSize; ++i) any |= secret[i];
  return any != 0;
}
inline void hexEncode(const uint8_t *bytes, size_t n, char *out) {
  static const char digits[] = "0123456789abcdef";
  for (size_t i = 0; i < n; ++i) { out[2*i] = digits[bytes[i] >> 4]; out[2*i+1] = digits[bytes[i] & 15]; }
  out[2*n] = 0;
}
// proof = first 16 bytes of HMAC-SHA256(secret, "SWM-AUTH-1" || challenge(16)
// || device serial ASCII (12) || host ID ASCII (36)).
inline bool computeProof(const uint8_t *secret, const uint8_t *challenge, const char *serial,
                         const char *host, uint8_t *proof) {
  constexpr size_t label = sizeof(authLabel) - 1;
  uint8_t message[label + challengeSize + serialSize + 36];
  memcpy(message, authLabel, label);
  memcpy(message + label, challenge, challengeSize);
  memcpy(message + label + challengeSize, serial, serialSize);
  memcpy(message + label + challengeSize + serialSize, host, 36);
  const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  uint8_t mac[32];
  if (!info || mbedtls_md_hmac(info, secret, secretSize, message, sizeof(message), mac) != 0) return false;
  memcpy(proof, mac, proofSize);
  return true;
}

struct PairedComputer {
  char host[37]{}; char name[21]{};
  uint8_t secret[secretSize]{};
  bool hasSecret = false;  // false only for a selection migrated from pre-secret firmware
  uint32_t lastUsed = 0;
};

// NVS "pairs" blob. Stable binary layout (little-endian, 4-byte aligned).
struct RegistryBlob {
  uint32_t magic = 0x32525053;  // "SPR2"
  uint8_t count = 0, selected = 0xff, reserved[2]{};
  uint32_t clock = 0;
  struct Entry {
    uint32_t lastUsed; char host[36]; uint8_t nameLength; char name[20]; uint8_t flags; uint8_t secret[secretSize];
  } entries[maxPaired]{};
  uint32_t checksum = 0;
};
static_assert(sizeof(RegistryBlob::Entry) == 96, "Registry entry layout must remain stable");
static_assert(sizeof(RegistryBlob) == 784, "Registry layout must remain stable");

class Registry {
 public:
  PairedComputer entries[maxPaired]{};
  unsigned count = 0;
  int selected = -1;

  const PairedComputer *current() const { return selected >= 0 ? &entries[selected] : nullptr; }
  int find(const char *host) const {
    for (unsigned i = 0; i < count; ++i) if (!memcmp(entries[i].host, host, 36)) return int(i);
    return -1;
  }
  // Adds or updates a computer; when full the least recently used one is replaced.
  int add(const char *host, const char *name, const uint8_t *secret, bool hasSecret) {
    int index = find(host);
    if (index < 0) {
      if (count < maxPaired) index = int(count++);
      else {
        index = 0;
        for (unsigned i = 1; i < count; ++i) if (entries[i].lastUsed < entries[index].lastUsed) index = int(i);
        if (selected == index) selected = -1;
      }
      entries[index] = PairedComputer{};
      memcpy(entries[index].host, host, 36);
    }
    PairedComputer &entry = entries[index];
    size_t nameLength = strnlen(name, 20);
    memset(entry.name, 0, sizeof(entry.name)); memcpy(entry.name, name, nameLength);
    if (hasSecret) { memcpy(entry.secret, secret, secretSize); entry.hasSecret = true; }
    entry.lastUsed = ++clock_;
    return index;
  }
  void select(int index) {
    if (index < 0 || unsigned(index) >= count) { selected = -1; return; }
    selected = index; entries[index].lastUsed = ++clock_;
  }
  void remove(int index) {
    if (index < 0 || unsigned(index) >= count) return;
    for (unsigned i = unsigned(index); i + 1 < count; ++i) entries[i] = entries[i + 1];
    entries[--count] = PairedComputer{};
    if (selected == index) selected = -1;
    else if (selected > index) --selected;
  }
  // Which paired computer produced this proof? Every stored secret is tried so
  // the proof never has to name a computer. -1 when none matches.
  int matchProof(const uint8_t *proof, const uint8_t *challenge, const char *serial) const {
    int match = -1;
    for (unsigned i = 0; i < count; ++i) {
      if (!entries[i].hasSecret) continue;
      uint8_t expected[proofSize];
      if (computeProof(entries[i].secret, challenge, serial, entries[i].host, expected) &&
          constantTimeEqual(expected, proof, proofSize) && match < 0) match = int(i);
    }
    return match;
  }
  RegistryBlob encode() const {
    RegistryBlob blob;
    memset(reinterpret_cast<uint8_t*>(blob.entries), 0, sizeof(blob.entries));  // deterministic padding
    blob.count = uint8_t(count); blob.selected = selected < 0 ? 0xff : uint8_t(selected); blob.clock = clock_;
    for (unsigned i = 0; i < count; ++i) {
      auto &out = blob.entries[i]; const auto &in = entries[i];
      out.lastUsed = in.lastUsed; memcpy(out.host, in.host, 36);
      out.nameLength = uint8_t(strnlen(in.name, 20)); memcpy(out.name, in.name, out.nameLength);
      out.flags = in.hasSecret ? 1 : 0;
      if (in.hasSecret) memcpy(out.secret, in.secret, secretSize);
    }
    blob.checksum = ::crc32(reinterpret_cast<const uint8_t*>(&blob), offsetof(RegistryBlob, checksum));
    return blob;
  }
  bool decode(const RegistryBlob &blob) {
    if (blob.magic != 0x32525053 || blob.count > maxPaired ||
        (blob.selected != 0xff && blob.selected >= blob.count) ||
        blob.checksum != ::crc32(reinterpret_cast<const uint8_t*>(&blob), offsetof(RegistryBlob, checksum)))
      return false;
    Registry next;
    for (unsigned i = 0; i < blob.count; ++i) {
      const auto &in = blob.entries[i]; auto &out = next.entries[i];
      if (!hostId(reinterpret_cast<const uint8_t*>(in.host), 36) ||
          !hostName(reinterpret_cast<const uint8_t*>(in.name), in.nameLength) || in.flags > 1 ||
          (in.flags && !usableSecret(in.secret)) || next.find(in.host) >= 0) return false;
      memcpy(out.host, in.host, 36); memcpy(out.name, in.name, in.nameLength);
      out.hasSecret = in.flags == 1;
      if (out.hasSecret) memcpy(out.secret, in.secret, secretSize);
      out.lastUsed = in.lastUsed;
      next.count = i + 1;
    }
    next.selected = blob.selected == 0xff ? -1 : int(blob.selected);
    next.clock_ = blob.clock;
    *this = next;
    return true;
  }
  // Pre-secret firmware stored one selected host (NVS `host`/`name`).
  void migrateLegacy(const char *host, size_t hostLength, const char *name) {
    *this = Registry{};
    if (!hostId(reinterpret_cast<const uint8_t*>(host), hostLength)) return;
    const char *label = hostName(reinterpret_cast<const uint8_t*>(name), strlen(name)) ? name : "Computer";
    select(add(host, label, nullptr, false));
  }

 private:
  uint32_t clock_ = 0;
};

struct AuthOutcome { uint8_t result; bool drop, changed; };  // aggregate (C++11 toolchain)

// Trust on first use for a selection inherited from pre-secret firmware is
// allowed only briefly: for ten minutes after each boot (an OTA install, a reset
// or an owner's wake), and only for the first legacy hello that names the
// stored computer. Afterwards the owner must pair again with the physical menu.
class LegacyWindow {
 public:
  static constexpr uint32_t windowMs = 10u * 60u * 1000u;
  // `now` is milliseconds since boot; call at least every few seconds so the
  // window latches closed long before millis() could wrap.
  bool open(uint32_t now) { if (now >= windowMs) closed_ = true; return !closed_; }
  void consume() { closed_ = true; }
 private:
  bool closed_ = false;
};

// Authorization state of one BLE link. Each link gets exactly one hello
// attempt against its fresh challenge; any rejection closes the link.
class LinkAuth {
 public:
  enum class State : uint8_t { Open, Provisioning, Authorized, Closed };
  State state = State::Open;
  void reset() { state = State::Open; }
  // Opening/closing the physical menu ends this link's authorization.
  void revoke() { state = State::Closed; }
  bool authorized() const { return state == State::Authorized; }

  // Legacy `H` is only a migration path: the selected host from pre-secret
  // firmware proves its ID once and must immediately provision a secret (Y).
  // `window` is the LegacyWindow; a matching hello consumes it (see there).
  AuthOutcome legacyHello(const uint8_t *p, size_t n, const Registry &registry, bool menu, bool ota,
                          LegacyWindow &window, uint32_t now) {
    if (ota) return current();
    if (state == State::Authorized) return {helloOk, false, false};
    bool valid = n >= 37 && n <= 57 && hostId(p + 1, 36) && (n == 37 || hostName(p + 37, n - 37));
    const PairedComputer *selected = registry.current();
    if (state != State::Open || menu || !valid || !selected || selected->hasSecret || memcmp(selected->host, p + 1, 36))
      return reject();
    if (!window.open(now)) return reject();
    window.consume();
    state = State::Provisioning;
    return {helloProvision, false, false};
  }
  // `P:u8, proof:16`. A paired computer that is not the selected one learns
  // that it is still paired (result 9) but is not authorized.
  AuthOutcome proofHello(const uint8_t *p, size_t n, const Registry &registry, const uint8_t *challenge,
                         const char *serial, bool menu, bool ota) {
    if (ota) return current();
    if (state == State::Authorized) return {helloOk, false, false};
    if (state != State::Open || menu || n != 1 + proofSize) return reject();
    int match = registry.matchProof(p + 1, challenge, serial);
    if (match < 0) return reject();
    if (match != registry.selected) { state = State::Closed; return {helloNotSelected, true, false}; }
    state = State::Authorized;
    return {helloOk, false, true};
  }
  // `Y:u8, secret:32` is legal only directly after an accepted legacy H.
  bool provisionAllowed(const uint8_t *p, size_t n) const {
    return state == State::Provisioning && n == 1 + secretSize && usableSecret(p + 1);
  }
  AuthOutcome provisionRejected() { return reject(); }
  AuthOutcome provisioned(bool stored) {
    if (!stored) { state = State::Closed; return {helloStoreFailed, true, false}; }
    state = State::Authorized;
    return {helloOk, false, true};
  }

 private:
  AuthOutcome reject() { state = State::Closed; return {helloRejected, true, false}; }
  AuthOutcome current() const { return {authorized() ? helloOk : helloRejected, false, false}; }
};
}  // namespace sweetmeter

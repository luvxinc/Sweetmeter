#pragma once
#include <stdint.h>

// Deadlines of a link that has not authorized yet (docs/PROTOCOL.md sections 1
// and 4) count from the moment it is encrypted. A computer pairing with the
// meter for the first time waits for its user: macOS asks "Connection Request"
// and starts pairing only after Connect is clicked, so the meter cannot see
// that pairing is coming. Until the link is encrypted it may stay for
// unencryptedLinkMs (the pairing protocol's own 30-second timeout); a stray
// central that never encrypts is still dropped then.
namespace sweetmeter {
constexpr uint32_t unencryptedLinkMs = 30000;

// True once `ms` have passed since `since`. The worker samples `now` before the
// Bluetooth task may record a later event, so a timestamp after `now` has not
// expired (an unsigned difference would wrap around to a huge age).
inline bool elapsedAtLeast(uint32_t now, uint32_t since, uint32_t ms) {
  int32_t age = int32_t(now - since);
  return age >= 0 && uint32_t(age) >= ms;
}

// `encryptedAt` is 0 while this link is not encrypted; only its first encryption
// counts. However often a central pairs again, a link that has not authorized
// never outlasts unencryptedLinkMs + limitMs after connecting.
inline bool linkDeadlinePassed(uint32_t now, uint32_t connectedAt, uint32_t encryptedAt, uint32_t limitMs) {
  if (elapsedAtLeast(now, connectedAt, unencryptedLinkMs + limitMs)) return true;
  if (encryptedAt) return elapsedAtLeast(now, encryptedAt, limitMs);
  return elapsedAtLeast(now, connectedAt, unencryptedLinkMs);
}
}  // namespace sweetmeter

// One colour per person. Derived from the user id, so the same person keeps
// their colour in the list and in the viewer; collisions inside one session
// are resolved by taking the next free colour.

export const COLORS = ['#6ea8fe', '#7ddc9b', '#e58fd0', '#f2c66d', '#ff9e7a', '#7fd6d6', '#b9a3ff', '#c7d36f'];

function hash(text) {
  let h = 2166136261;
  for (const ch of String(text)) {
    h ^= ch.charCodeAt(0);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/** Map user id -> colour for the people of one session (order matters for collisions). */
export function assignColors(userIds) {
  const taken = new Set();
  const out = new Map();
  for (const id of userIds) {
    if (out.has(id)) continue;
    let slot = hash(id) % COLORS.length;
    for (let i = 0; i < COLORS.length && taken.has(slot); i++) slot = (slot + 1) % COLORS.length;
    taken.add(slot);
    out.set(id, COLORS[slot]);
  }
  return out;
}

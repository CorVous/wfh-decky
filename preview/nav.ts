// Approximate Steam gamepad navigation for the preview harness (dev only).
//
// Model (what the mocks in mock-ui.tsx mark up):
//   leaf   [data-nav-leaf]  a focus target: DialogButton, Dropdown, toggle row, slider, text
//          input, Field with onActivate, Focusable with onActivate. A "leaf" that contains
//          other leaves acts as a group instead (Steam focuses the children).
//   group  [data-nav-group] a Focusable (value = its flow-children), Field, PanelSection.
//          "horizontal"/"row" groups move between their children with left/right,
//          "grid" moves geometrically, anything else (no flow-children) with up/down.
// A d-pad press walks up from the focused leaf to the nearest group whose axis matches the
// direction and that has a next child that way, then focuses the leaf in that child
// closest to the current one (Steam remembers the last focused child; we use geometry).
// This mirrors how @decky/ui Focusable nesting behaves on the Deck closely enough to find
// controls the d-pad cannot reach, but it is not Steam's exact algorithm.
//
// Keys: arrows = d-pad, Enter = A, Escape/Backspace = B (closes modals), Q/E = L1/R1.

type Dir = "up" | "down" | "left" | "right";

function visible(el: Element): boolean {
  const r = (el as HTMLElement).getClientRects();
  if (!r.length) return false;
  const s = getComputedStyle(el as HTMLElement);
  return s.visibility !== "hidden" && s.display !== "none";
}

function hasLeafInside(el: Element): boolean {
  for (const c of el.querySelectorAll("[data-nav-leaf]")) if (c !== el && visible(c)) return true;
  return false;
}

export function isLeaf(el: Element): boolean {
  return el.hasAttribute("data-nav-leaf") && el.getAttribute("aria-disabled") !== "true" && !(el as HTMLInputElement).disabled && visible(el) && !hasLeafInside(el);
}

function isGroup(el: Element): boolean {
  return el.hasAttribute("data-nav-group") || (el.hasAttribute("data-nav-leaf") && hasLeafInside(el));
}

export function navRoot(): Element | null {
  const modals = document.querySelectorAll(".pv-modal");
  if (modals.length) return modals[modals.length - 1];
  return document.querySelector("[data-nav-root]");
}

function navParent(el: Element, root: Element): Element | null {
  let p = el.parentElement;
  while (p && p !== root) {
    if (isGroup(p)) return p;
    p = p.parentElement;
  }
  return p === root ? root : null;
}

function axis(group: Element): "h" | "v" | "grid" {
  const f = (group.getAttribute("data-nav-group") || "").toLowerCase();
  if (f === "horizontal" || f === "row") return "h";
  if (f === "grid" || f === "geometric") return "grid";
  return "v";
}

export function leaves(root: Element): Element[] {
  return Array.from(root.querySelectorAll("[data-nav-leaf]")).filter(isLeaf);
}

function navChildren(group: Element, root: Element): Element[] {
  const out: Element[] = [];
  for (const el of group.querySelectorAll("[data-nav-leaf],[data-nav-group]")) {
    if (navParent(el, root) !== group) continue;
    if (isLeaf(el) || (isGroup(el) && leaves(el).length)) out.push(el);
  }
  return out;
}

function center(el: Element) {
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2, r };
}

function closestLeaf(cands: Element[], from: Element, dir: Dir): Element | null {
  const f = center(from);
  let best: Element | null = null;
  let bestD = Infinity;
  for (const c of cands) {
    const p = center(c);
    const along = dir === "up" || dir === "down" ? Math.abs(p.y - f.y) : Math.abs(p.x - f.x);
    // overlapping on the perpendicular axis is nearly aligned; a gap costs a lot
    const vert = dir === "up" || dir === "down";
    const gap = vert
      ? Math.max(0, Math.max(p.r.left, f.r.left) - Math.min(p.r.right, f.r.right))
      : Math.max(0, Math.max(p.r.top, f.r.top) - Math.min(p.r.bottom, f.r.bottom));
    const off = vert ? Math.abs(p.x - f.x) : Math.abs(p.y - f.y);
    const d = (gap ? gap * 4 + off : off * 0.25) + along;
    if (d < bestD) {
      bestD = d;
      best = c;
    }
  }
  return best;
}

function inDirection(el: Element, from: Element, dir: Dir): boolean {
  const a = el.getBoundingClientRect();
  const b = from.getBoundingClientRect();
  if (dir === "down") return a.top >= b.bottom - 2;
  if (dir === "up") return a.bottom <= b.top + 2;
  if (dir === "right") return a.left >= b.right - 2;
  return a.right <= b.left + 2;
}

export function next(from: Element, dir: Dir, root: Element | null = navRoot()): Element | null {
  if (!root) return null;
  const want = dir === "left" || dir === "right" ? "h" : "v";
  const step = dir === "down" || dir === "right" ? 1 : -1;
  let node: Element = from;
  for (let guard = 0; guard < 200; guard++) {
    const group = navParent(node, root);
    if (!group) return null;
    const ax = axis(group);
    if (ax === "grid") {
      const cands = leaves(group).filter((l) => l !== from && inDirection(l, from, dir));
      const hit = closestLeaf(cands, from, dir);
      if (hit) return hit;
    } else if (ax === want) {
      const kids = navChildren(group, root);
      let i = kids.findIndex((k) => k === node || k.contains(node));
      if (i >= 0) {
        for (i += step; i >= 0 && i < kids.length; i += step) {
          const k = kids[i];
          const ls = isLeaf(k) ? [k] : leaves(k);
          if (ls.length) return closestLeaf(ls, from, dir);
        }
      }
    }
    if (group === root) return null;
    node = group;
  }
  return null;
}

function describe(el: Element): string {
  const t = ((el as HTMLElement).innerText || (el as HTMLInputElement).value || el.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
  const r = el.getBoundingClientRect();
  const kind = el.classList.contains("pv-btn") ? "button" : el.tagName === "INPUT" ? "input" : el.getAttribute("role") || (el.classList.contains("pv-field") ? "field" : "focusable");
  return `${kind} "${t.slice(0, 48)}" @${Math.round(r.left)},${Math.round(r.top)}`;
}

// Every leaf reachable with the d-pad from the first one (Steam focuses the first on entry),
// plus direction sanity: "blocked" = a press that goes nowhere although a control lies in that
// direction on the same row/column; "wrongWay" = a press that lands on a control that is not
// in that direction at all (e.g. down moves sideways in a row that lacks flow-children).
function sameBand(a: Element, b: Element, dir: Dir): boolean {
  const r = a.getBoundingClientRect();
  const s = b.getBoundingClientRect();
  return dir === "up" || dir === "down" ? Math.min(r.right, s.right) - Math.max(r.left, s.left) > 4 : Math.min(r.bottom, s.bottom) - Math.max(r.top, s.top) > 4;
}

export function reachability(root: Element | null = navRoot()) {
  const empty = { total: 0, reached: 0, unreachable: [] as string[], blocked: [] as string[], wrongWay: [] as string[] };
  if (!root) return empty;
  const all = leaves(root);
  if (!all.length) return empty;
  const seen = new Set<Element>([all[0]]);
  const queue = [all[0]];
  const blocked: string[] = [];
  const wrongWay: string[] = [];
  while (queue.length) {
    const cur = queue.shift()!;
    for (const d of ["up", "down", "left", "right"] as Dir[]) {
      const n = next(cur, d, root);
      if (!n) {
        const there = all.find((l) => l !== cur && inDirection(l, cur, d) && sameBand(l, cur, d));
        if (there) blocked.push(`${d} from ${describe(cur)} -> nothing (${describe(there)} is there)`);
      } else if (!inDirection(n, cur, d)) {
        wrongWay.push(`${d} from ${describe(cur)} -> ${describe(n)}`);
      }
      if (n && !seen.has(n)) {
        seen.add(n);
        queue.push(n);
      }
    }
  }
  const unreachable = all.filter((l) => !seen.has(l)).map(describe);
  return { total: all.length, reached: seen.size, unreachable, blocked, wrongWay };
}

export function installKeyboard(): void {
  window.addEventListener("keydown", (e) => {
    const map: Record<string, Dir> = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" };
    const dir = map[e.key];
    const root = navRoot();
    if (!root) return;
    const act = document.activeElement;
    if (dir) {
      if (act?.tagName === "INPUT" && (dir === "left" || dir === "right")) return;
      e.preventDefault();
      const cur = act && root.contains(act) && isLeaf(act) ? act : null;
      const target = cur ? next(cur, dir, root) : leaves(root)[0];
      if (target) {
        (target as HTMLElement).focus({ preventScroll: true });
        (target as HTMLElement).scrollIntoView({ block: "nearest", inline: "nearest" });
      }
    } else if (e.key === "Enter" && act && act !== document.body && act.tagName !== "INPUT") {
      e.preventDefault();
      (act as HTMLElement).click();
    }
  });
}

(window as any).__nav = { next, reachability, leaves: () => leaves(navRoot()!), navRoot };

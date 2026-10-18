// Preview harness entry (dev only). Pins the browser clock to the bridge's clock first,
// then loads the app (dynamic import, so src/ modules evaluate with the shifted Date).
import { installKeyboard } from "./nav";

async function pinClock(): Promise<void> {
  try {
    const r = await fetch("/clock");
    const { now_ms } = await r.json();
    const RealDate = Date;
    const offset = now_ms - RealDate.now();
    if (Math.abs(offset) < 1000) return;
    class PinnedDate extends RealDate {
      constructor(...a: any[]) {
        if (a.length === 0) super(RealDate.now() + offset);
        else super(...(a as [any]));
      }
      static now() {
        return RealDate.now() + offset;
      }
    }
    (globalThis as any).Date = PinnedDate;
  } catch {
    // no bridge clock: keep wall time
  }
}

async function main() {
  await pinClock();
  installKeyboard();
  const { mount } = await import("./app");
  mount(document.getElementById("root")!);
}

main();

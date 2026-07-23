// Mock data source: ticks the SimModel and pushes each StateFrame into the shared
// store, exactly as ws.js does for the live /ws stream. Presets subscribe to the
// store and never know (or care) whether frames come from here or from real bands.

import { setFrame, setConnected } from "../store.js";
import { SimModel } from "./model.js";

export function startMockSource({ hz = 10, people = 6 } = {}) {
  const model = new SimModel(30);
  model.setActiveCount(people);
  setConnected(true);
  const timer = setInterval(() => setFrame(model.step()), 1000 / hz);
  return {
    model,
    stop() { clearInterval(timer); setConnected(false); },
  };
}

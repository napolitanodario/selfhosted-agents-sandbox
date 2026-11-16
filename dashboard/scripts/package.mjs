import { cp, mkdir, rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";

// The Python wheel serves the compiled SPA. No Node runtime is needed on the host.
const destination = fileURLToPath(
  new URL("../../src/experiment_dashboard/static/", import.meta.url),
);
await rm(destination, { recursive: true, force: true });
await mkdir(destination, { recursive: true });
await cp(new URL("../build/client/", import.meta.url), destination, {
  recursive: true,
});

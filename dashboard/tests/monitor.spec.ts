import { test, expect, type Page } from "@playwright/test";

test("monitoring reconnects after lost connectivity and deep links remain authenticated", async ({
  page,
}) => {
  await page.goto("/agents?session=session0");
  await page
    .getByLabel("Workspace access token")
    .fill("test-dashboard-access-token-only");
  await page.getByRole("button", { name: "Open workspace" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("button", { name: "Close details" }).click();
  await expect(
    page.getByText("Live connection", { exact: true }),
  ).toBeVisible();
  await page.context().setOffline(true);
  await expect(page.getByText("Reconnecting", { exact: true })).toBeVisible({
    timeout: 10_000,
  });
  await page.context().setOffline(false);
  await expect(page.getByText("Live connection", { exact: true })).toBeVisible({
    timeout: 10_000,
  });
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Agent sessions", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByLabel("Workspace access token")).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Workspace access token")).toBeVisible();
  expect((await page.request.get("/api/snapshot")).status()).toBe(401);
});

async function navigate(page: Page, name: RegExp) {
  const menu = page.getByRole("button", { name: "Open navigation" });
  if (await menu.isVisible()) await menu.click();
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name })
    .click();
}

test("authenticated monitoring, session logs, artifacts, comparison, and project isolation", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().includes("401"))
      errors.push(message.text());
  });
  await page.goto("/");
  await page
    .getByLabel("Workspace access token")
    .fill("test-dashboard-access-token-only");
  await page.getByRole("button", { name: "Open workspace" }).click();
  await expect(
    page.getByRole("heading", { name: "Overview", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Live connection", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Agent activity", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("overview.png"),
    fullPage: true,
  });

  await navigate(page, /^Agent sessions/);
  await page.getByRole("button", { name: /Proposer scripted/ }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByRole("log")).toContainText(
    "Comparing the baseline and candidate evidence.",
  );
  await expect(page.getByText("Synthetic test response")).toBeVisible();
  await page.getByRole("button", { name: "Close details" }).click();

  await navigate(page, /^Workloads/);
  await page.getByRole("button", { name: /scene-context-baseline/ }).click();
  await expect(
    page.getByText("Remote cluster workload", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: /result.json/ }).click();
  await expect(page.locator(".artifact-preview")).toContainText(
    '"quality": 0.76',
  );
  await page.getByRole("button", { name: "Close details" }).click();

  await navigate(page, /^Compare runs/);
  await expect(
    page.getByRole("heading", { name: "Recorded results", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "quality", exact: true }),
  ).toBeVisible();

  await navigate(page, /^Workloads/);
  const menu = page.getByRole("button", { name: "Open navigation" });
  if (await menu.isVisible()) await menu.click();
  await page.getByLabel("PROJECT SCOPE").selectOption("retrieval-lab");
  if (
    await page.getByRole("button", { name: "Close navigation" }).isVisible()
  ) {
    await page
      .getByRole("button", { name: "Close navigation" })
      .click({ position: { x: 350, y: 500 } });
  }
  await expect(
    page.getByRole("button", { name: /embedding-baseline/ }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /scene-context-baseline/ }),
  ).toHaveCount(0);

  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export snapshot" }).click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe("observatory-snapshot.json");
  const stream = await download.createReadStream();
  let data = "";
  for await (const chunk of stream!) data += chunk.toString();
  expect(
    JSON.parse(data).runs.every(
      (run: { project_id: string }) => run.project_id === "retrieval-lab",
    ),
  ).toBe(true);
  expect(data).not.toContain("fixture-secret-value");
  expect(
    (await page.request.post("/api/runs/baseline001/cancel")).status(),
  ).toBe(405);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  expect(errors).toEqual([]);
});

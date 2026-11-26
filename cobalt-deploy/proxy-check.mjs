import { fetch, ProxyAgent } from "undici";

const proxyUrl = process.env.YOUTUBE_PROXY_URL;
if (!proxyUrl) {
  console.error("[proxy-check] YOUTUBE_PROXY_URL is missing");
  process.exit(1);
}

const dispatcher = new ProxyAgent(proxyUrl);
try {
  const response = await fetch("https://www.youtube.com/generate_204", {
    dispatcher,
    headers: { "User-Agent": "Mozilla/5.0" },
    signal: AbortSignal.timeout(10000),
  });
  console.log("[proxy-check] youtube_generate_204_http=" + response.status);
  if (response.status !== 204) process.exitCode = 1;
} catch (error) {
  console.error("[proxy-check] failed:", error?.code || error?.message || String(error));
  process.exitCode = 1;
} finally {
  await dispatcher.close();
}

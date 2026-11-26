import { Innertube } from "youtubei.js";

const hardStop = setTimeout(() => {
  console.log("[YT_CLIENT_SMOKE] global timeout; continuing service startup");
  process.exit(0);
}, 30000);

const withTimeout = (promise, label, ms = 8000) => new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error(label + " timeout")), ms);
  promise.then(
    value => { clearTimeout(timer); resolve(value); },
    error => { clearTimeout(timer); reject(error); }
  );
});

const tests = [
  { id: "FgBo1KlkQQk", label: "requested_shorts" },
  { id: "LXb3EKWsInQ", label: "4k60_control" },
];

const clients = ["ANDROID", "ANDROID_VR", "TV", "IOS"];
let yt;
try {
  yt = await withTimeout(Innertube.create({ retrieve_player: true }), 'Innertube.create');
} catch (error) {
  console.log('[YT_CLIENT_SMOKE] create failed: ' + String(error?.message || error));
  clearTimeout(hardStop);
  process.exit(0);
}

for (const client of clients) {
  for (const test of tests) {
    try {
      const info = await withTimeout(yt.getBasicInfo(test.id, { client }), client + '/' + test.label);
      const data = info.streaming_data || {};
      const formats = [
        ...(data.adaptive_formats || []),
        ...(data.formats || []),
      ];
      const maxHeight = formats.reduce((max, f) => Math.max(max, Number(f.height) || 0), 0);
      const maxFps = formats.reduce((max, f) => Math.max(max, Number(f.fps) || 0), 0);
      const title = info.basic_info?.title || info.video_details?.title || "";
      const playability = info.playability_status || {};
      console.log(
        "[YT_CLIENT_SMOKE] label=" + test.label +
        " client=" + client +
        " status=ok maxHeight=" + maxHeight +
        " maxFps=" + maxFps +
        " formats=" + formats.length +
        " playability=" + JSON.stringify({status: playability.status, reason: playability.reason}) +
        " title=" + JSON.stringify(title)
      );
    } catch (error) {
      console.log(
        "[YT_CLIENT_SMOKE] label=" + test.label +
        " client=" + client +
        " status=fail error=" + String(error?.message || error).slice(0, 220)
      );
    }
  }
}
clearTimeout(hardStop);

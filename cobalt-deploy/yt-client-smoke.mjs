import { Innertube } from "youtubei.js";

const tests = [
  { id: "FgBo1KlkQQk", label: "requested_shorts" },
  { id: "LXb3EKWsInQ", label: "4k60_control" },
];

const clients = ["ANDROID", "ANDROID_VR", "TV", "IOS"];
const yt = await Innertube.create({ retrieve_player: true });

for (const client of clients) {
  for (const test of tests) {
    try {
      const info = await yt.getBasicInfo(test.id, { client });
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

// Cloudflare Worker: stößt den Sentinel-Workflow stündlich an und den Daten-Workflow von Cyberlage alle 2 Stunden (plus zwei Morgenläufe).
// Grund: GitHubs eigener Zeitplan (schedule) läuft bei diesem Repo oft stundenlang zu spät.
// Der Worker braucht ein Secret GH_TOKEN (fine-grained token, nur diese beiden Repos (sentinel3, cyberlage.org), Berechtigung "Actions: Read and write").
const CYBERLAGE_CRONS = ["12 */2 * * *", "45 4 * * *", "15 5 * * *"];

export default {
  async scheduled(event, env, ctx) {
    // Cyberlage-Zeiten sind eigene Cron-Einträge (wrangler.jsonc); alles andere ist der Sentinel-Takt
    if (CYBERLAGE_CRONS.includes(event.cron)) ctx.waitUntil(dispatch(env, "cyberlage.org", "daten.yml", {}, "cyberlage-ticker"));
    else ctx.waitUntil(trigger(env));
  },
  // Manueller Test im Browser: /?key=<TEST_KEY> (nur wenn TEST_KEY als Secret gesetzt ist)
  async fetch(request, env) {
    const key = new URL(request.url).searchParams.get("key");
    if (!env.TEST_KEY || key !== env.TEST_KEY) return new Response("ok", { status: 200 });
    return new Response(await trigger(env), { status: 200 });
  },
};

async function trigger(env) {
  return dispatch(env, "sentinel3", "sentinel.yml", { mode: "tick" }, "sentinel3-ticker");
}

async function dispatch(env, repo, workflow, inputs, agent) {
  const res = await fetch(
    `https://api.github.com/repos/maxim91136/${repo}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": agent,
      },
      body: JSON.stringify({ ref: "main", inputs }),
    }
  );
  const msg = `${repo}: GitHub antwortet ${res.status}` + (res.status === 204 ? " (angestoßen)" : ": " + (await res.text()).slice(0, 200));
  console.log(msg);
  return msg;
}

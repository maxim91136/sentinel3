// Cloudflare Worker: stößt den Sentinel-Workflow stündlich an.
// Grund: GitHubs eigener Zeitplan (schedule) läuft bei diesem Repo oft stundenlang zu spät.
// Der Worker braucht ein Secret GH_TOKEN (fine-grained token, nur dieses Repo, Berechtigung "Actions: Read and write").
export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(trigger(env));
  },
  // Manueller Test im Browser: /?key=<TEST_KEY> (nur wenn TEST_KEY als Secret gesetzt ist)
  async fetch(request, env) {
    const key = new URL(request.url).searchParams.get("key");
    if (!env.TEST_KEY || key !== env.TEST_KEY) return new Response("ok", { status: 200 });
    return new Response(await trigger(env), { status: 200 });
  },
};

async function trigger(env) {
  const res = await fetch(
    "https://api.github.com/repos/maxim91136/sentinel3/actions/workflows/sentinel.yml/dispatches",
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "sentinel3-ticker",
      },
      body: JSON.stringify({ ref: "main", inputs: { mode: "tick" } }),
    }
  );
  const msg = `GitHub antwortet ${res.status}` + (res.status === 204 ? " (angestoßen)" : ": " + (await res.text()).slice(0, 200));
  console.log(msg);
  return msg;
}

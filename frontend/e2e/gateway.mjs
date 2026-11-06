// Test-only same-origin gateway; production routing belongs to Nginx.
import http from "node:http";

const [webPort, nextPort, fixturePort] = process.argv.slice(2).map(Number);

http
  .createServer((request, response) => {
    const isApi = request.url === "/api/v1" || request.url.startsWith("/api/v1/");
    const upstream = http.request(
      {
        hostname: "127.0.0.1",
        port: isApi ? fixturePort : nextPort,
        path: isApi ? request.url.slice(4) : request.url,
        method: request.method,
        headers: request.headers,
      },
      (upstreamResponse) => {
        response.writeHead(upstreamResponse.statusCode, upstreamResponse.headers);
        upstreamResponse.pipe(response);
      },
    );
    upstream.on("error", () => {
      if (!response.headersSent) response.writeHead(502);
      response.end();
    });
    response.on("close", () => upstream.destroy());
    request.pipe(upstream);
  })
  .listen(webPort, "127.0.0.1");

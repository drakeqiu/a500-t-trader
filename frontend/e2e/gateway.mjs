// Same-origin gateway for frontend development and browser tests; production uses Nginx.
import http from "node:http";

const [webPort, nextPort, fixturePort, bindHost = "127.0.0.1"] = process.argv
  .slice(2, 6)
  .map((value, index) => (index < 3 ? Number(value) : value));
const apiHost = (request) => request.url === "/api/v1" || request.url.startsWith("/api/v1/");
const fixtureHost = (request) => request.url.startsWith("/fixture-idp/");

const server = http.createServer((request, response) => {
  const isApi = apiHost(request);
  const isFixture = fixtureHost(request);
  const upstreamPort = isApi || isFixture ? fixturePort : nextPort;
  const upstreamPath = isApi ? request.url.slice(4) : request.url;
  const upstream = http.request(
    {
      hostname: "127.0.0.1",
      port: upstreamPort,
      path: upstreamPath,
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
});

// Next's development client waits for its HMR WebSocket before hydrating.
server.on("upgrade", (request, socket, head) => {
  const upstream = http.request({
    hostname: "127.0.0.1",
    port: nextPort,
    path: request.url,
    headers: request.headers,
  });
  upstream.on("upgrade", (response, upstreamSocket, upstreamHead) => {
    socket.on("error", () => upstreamSocket.destroy());
    upstreamSocket.on("error", () => socket.destroy());
    socket.write(
      `HTTP/${response.httpVersion} ${response.statusCode} ${response.statusMessage}\r\n` +
        response.rawHeaders.reduce(
          (headers, value, index, all) =>
            index % 2 === 0 ? `${headers}${value}: ${all[index + 1]}\r\n` : headers,
          "",
        ) +
        "\r\n",
    );
    if (head.length) upstreamSocket.write(head);
    if (upstreamHead.length) socket.write(upstreamHead);
    upstreamSocket.pipe(socket);
    socket.pipe(upstreamSocket);
  });
  upstream.on("error", () => socket.destroy());
  upstream.end();
});

server.listen(webPort, bindHost);

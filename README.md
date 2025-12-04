# tiny-instancer

v2 branch is very different from v1, all of this is outdated :upside_down:

A tiny CTF challenge instancer with docker backend I wrote in one night.

## Why

Initially, for [osu!gaming CTF 2025](https://ctftime.org/event/2801) we wanted to reduce the costs of running the infra,
because we were paying for it out of our own pocket (we were lazy to apply for gcp sponsorship).

The idea was to just get one beefy dedi on hetzner and just host the instancer there, which is essentially what we did,
and it worked pretty well to say the least! I don't think we experienced any issues with it except a misconfigured 
docker address pool, which was fixed in the first 30 minutes of the CTF.

![grafana screenshot](https://i.imgur.com/U7DJPd7.png)

*(the 0 values here are when I was tinkering with prometheus so it wasn't collecting metrics)*

So, if you are planning on hosting a tiny CTF with infra paid out of pocket, you may consider using the tiny-instancer!

## Features

- Unlimited amounts of containers per challenge, just describe them in the yaml.
- Private network for each instance, essentially isolating all instances.
- DNS resolving of specific containers in the network by their container name described in yaml.
- Security hardenings built-in.

## Challenge configuration

Let's take a look at this example of the challenge we had:
```yaml
name: 'barcade'
timeout: 900
containers:
  - name: 'app'
    image: 'osu-ctf-challenges/barcade:latest'
    limits:
      memory: '256Mi'
      cpu: '1.0'
    security:
      read_only_fs: false
  - name: 'nginx'
    image: 'osu-ctf-challenges/barcade-nginx:latest'
    security:
      read_only_fs: false
      cap_add:
        - CAP_CHOWN
        - CAP_FOWNER
        - CAP_SETGID
        - CAP_SETUID
expose:
  - kind: 'https'
    container_name: 'nginx'
    container_port: 1337
```

Here we are setting up multiple containers, one for reverse proxy and one with the app itself. We are also limiting
the memory/cpu so the machine doesn't blow up in case things go south. I feel like the yaml is pretty self-explanatory.

If you're curious about all the options we have, here's an example full configuration (with default values, if there
are any).

```yaml
name:
timeout:
containers:
  - name:
    image:
    env: {}
    egress: false
    security:
      read_only_fs: true
      security_opt:
        - 'no-new-privileges'
      cap_add: []
      cap_drop:
        - 'ALL'
    limits:
      memory: '512Mi'
      cpu: '0.5'
      pids_limit: 1024
      ulimits:
        - name: 'nofile'
          soft: 1024
          hard: 1024
expose:
  - kind: 'http/https/tcp'
    container_name:
    container_port:
```

Pretty customizable, right?

## How it works

So, the app does not have any database, nor does it really need redis, but it is used here solely for the purpose of 
caching the tokens, so you don't request the CTF platform every time.

The way it works is, it creates docker resources with specific labels, and then queries these resources by the labels.
For example, when you're deploying a challenge it will set up:
- An internal network (with no internet access) with these labels:
    ```json
    { 
      "io.es3n1n.managed_by": "tiny-instancer",
      "io.es3n1n.instancer.expires_at": "1761558311"
    }
    ```
- A second network for egress (if its enabled) with the same labels.
- A container with these labels:
    ```json
    {
      "io.es3n1n.managed_by": "tiny-instancer",
      "io.es3n1n.instancer.team_id": "local",
      "io.es3n1n.instancer.started_at": "1761557411",
      "io.es3n1n.instancer.instance_id": "0ea5ef20c3ed",
      "io.es3n1n.instancer.hostname": "chart-viewer-0ea5ef20c3ed.instancer.es3n1n.eu",
      "io.es3n1n.instancer.expires_at": "1761558311",
      "io.es3n1n.instancer.challenge": "chart-viewer"
    } 
    ```

Then a periodic task will query all expired resources and clean them up based on their expiration time.

For routing we are using traefik because it's very straightforward and allows discovering endpoints based on the same
docker labels!

So to route, for example, https traffic to the app container, the instancer will add these labels:
```json
{
  "traefik.docker.network": "$internal_network_name",
  "traefik.enable": "true",

  "traefik.http.routers.$instance_id-app-0.entrypoints": "websecure",
  "traefik.http.routers.$instance_id-app-0.rule": "Host(`chart-viewer-0ea5ef20c3ed.instancer.es3n1n.eu`)",
  "traefik.http.routers.$instance_id-app-0.service": "$instance_id-app-0",
  "traefik.http.routers.$instance_id-app-0.tls": "true",

  "traefik.http.routers.$instance_id-app-0-redirect.entrypoints": "web",
  "traefik.http.routers.$instance_id-app-0-redirect.middlewares": "permanent-https-redirect@file",
  "traefik.http.routers.$instance_id-app-0-redirect.rule": "Host(`chart-viewer-0ea5ef20c3ed.instancer.es3n1n.eu`)",
  
  "traefik.http.services.$instance_id-app-0.loadbalancer.server.port": "3000"
}
```

Apart from routing the https, the instancer will detect if there are no http exposed ports, and will automatically add
the http redirect from http to https for this resource.

Very useful and neat!

## Platforms authorization

Since we allow one instance per team, we need to authorize people and tag instances based on their team ID. For 
debugging purposes there's a `local` auth provider, that isn't doing any authorization and just returns that static id 
`local`, so you can run the thing locally.

For production, we only have a rctf auth provider because that is exactly the platform we used.

## Docker configuration

For production, you should configure the default address pool of docker to make sure you will not run out of subnets for
the networks. 

To do that, you need to add this to your `/etc/docker/daemon.json`:
```json
{
  "default-address-pools": [
    {
      "base": "100.64.0.0/10",
      "size": 24
    }
  ]
}
```

This should limit the number of networks to 16384, which is ~8192 instances worst case. You can always add more subnets
here if you want.

After these changes, make sure to restart the docker.

## App configuration, and other deployment stuff

The only thing you really need to do is issue certs for `*.instancer.domain,instancer.domain`, place them in the certs 
folder (`fullchain.pem` and `privkey.pem`), then fill out the `.env` file in the same way as `.env.example`.

After that, configure the challenges in `challenges.yaml` and just do `docker compose up --build -d`.

You will then need to add DNS records for `*.instancer.domain` and `instancer.domain` to point at the machine where
you're hosting the application. Make sure to not use cloudflare proxy unless you're paying them for specific edge certs.

## Acknowledgments

[TJCSec/klodd](https://github.com/TJCSec/klodd) - **HEAVY** inspiration :kekw:

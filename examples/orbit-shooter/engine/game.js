(() => {
  const R = globalThis.OrbitRules;
  function start() {
    return {phase: "playing", x: R.width / 2, y: R.height - 75, life: R.shields,
      score: 0, elapsed: 0, cooldown: 0, grace: 0, spawn: 0.35, shots: [], rocks: [], bursts: []};
  }
  function pause(s) {
    if (s.phase === "playing") s.phase = "paused";
    else if (s.phase === "paused") s.phase = "playing";
  }
  const bounded = (x, low, high) => Math.max(low, Math.min(high, x));
  function step(s, input = {}, seconds = 1 / 60, random = Math.random) {
    if (s.phase !== "playing") return;
    const dt = bounded(seconds, 0, 0.05);
    s.elapsed += dt;
    const dx = input.x || 0, dy = input.y || 0;
    const length = Math.max(1, Math.hypot(dx, dy));
    s.x = bounded(s.x + dx / length * R.speed * dt, 17, R.width - 17);
    s.y = bounded(s.y + dy / length * R.speed * dt, 24, R.height - 20);
    s.grace = Math.max(0, s.grace - dt);
    s.cooldown -= dt;
    if (input.fire && s.cooldown <= 0) {
      s.shots.push({x: s.x, y: s.y - 22});
      s.cooldown = R.cooldown;
    }
    s.spawn -= dt;
    if (s.spawn <= 0) {
      const level = Math.min(s.score / 1000, 6);
      const r = 13 + random() * 16;
      s.rocks.push({x: r + random() * (R.width - 2*r), y: -r, r,
        speed: 90 + random() * 50 + level * 22, angle: random() * 6});
      s.spawn = Math.max(0.28, 0.92 - level * 0.095);
    }
    for (const shot of s.shots) shot.y -= R.shotSpeed * dt;
    for (const rock of s.rocks) {
      rock.y += rock.speed * dt;
      rock.angle += dt * 0.8;
      const shot = s.shots.find(b => !b.used && Math.hypot(b.x-rock.x, b.y-rock.y) <= rock.r + 3);
      if (shot) {
        shot.used = rock.gone = true;
        s.score += R.points;
        s.bursts.push({x: rock.x, y: rock.y, t: 0});
      } else if (s.grace <= 0 && Math.hypot(s.x-rock.x, s.y-rock.y) < rock.r + 10) {
        rock.gone = true;
        s.life--;
        s.grace = R.protection;
        if (!s.life) { s.phase = "over"; break; }
      }
    }
    s.shots = s.shots.filter(b => !b.used && b.y > -12);
    s.rocks = s.rocks.filter(r => !r.gone && r.y < R.height + r.r);
    s.bursts.forEach(b => b.t += dt);
    s.bursts = s.bursts.filter(b => b.t < 0.32);
  }
  globalThis.OrbitGame = {start, pause, step};
})();

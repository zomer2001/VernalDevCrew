(() => {
  const $ = id => document.getElementById(id);
  const canvas = document.querySelector("canvas"), ctx = canvas.getContext("2d");
  const {width:w, height:h} = OrbitRules;
  const keys = new Set();
  let pointer = null, last = 0, best = 0, s = OrbitGame.start();
  try { best = Number(localStorage.getItem("orbit-patrol.best")) || 0; } catch {}
  s.phase = "ready";
  function controls() {
    $("cover").hidden = s.phase === "playing";
    $("message").textContent = {ready:"准备起航", paused:"航行暂停", over:"本次航行结束"}[s.phase] || "";
    $("launch").textContent = s.phase === "paused" ? "继续" : s.phase === "over" ? "再次起航" : "开始游戏";
    $("pause").disabled = !["playing","paused"].includes(s.phase);
    $("pause").textContent = s.phase === "paused" ? "继续" : "暂停";
    $("restart").disabled = s.phase === "ready";
  }
  function reset() { s = OrbitGame.start(); keys.clear(); pointer = null; controls(); }
  function toggle() { OrbitGame.pause(s); keys.clear(); pointer = null; controls(); }
  $("launch").onclick = () => s.phase === "paused" ? toggle() : reset();
  $("pause").onclick = toggle;
  $("restart").onclick = reset;
  const codes = ["ArrowLeft","ArrowRight","ArrowUp","ArrowDown","Space","KeyW","KeyA","KeyS","KeyD"];
  addEventListener("keydown", e => {
    if (e.code === "KeyP" && !e.repeat) toggle();
    if (codes.includes(e.code) && s.phase === "playing") { e.preventDefault(); keys.add(e.code); }
  });
  addEventListener("keyup", e => keys.delete(e.code));
  addEventListener("blur", () => { if (s.phase === "playing") toggle(); });
  function touch(e) {
    const r = canvas.getBoundingClientRect();
    pointer = {x:(e.clientX-r.left)*w/r.width, y:(e.clientY-r.top)*h/r.height - 22};
  }
  canvas.onpointerdown = e => { canvas.setPointerCapture(e.pointerId); touch(e); };
  canvas.onpointermove = e => { if (canvas.hasPointerCapture(e.pointerId)) touch(e); };
  canvas.onlostpointercapture = () => { pointer = null; };
  function shape(points, fill, stroke) {
    ctx.beginPath(); points.forEach(([x,y],i) => i ? ctx.lineTo(x,y) : ctx.moveTo(x,y));
    ctx.closePath(); ctx.fillStyle=fill; ctx.fill(); ctx.strokeStyle=stroke; ctx.stroke();
  }
  function draw() {
    ctx.fillStyle="#0c1925"; ctx.fillRect(0,0,w,h);
    for(let i=0;i<115;i++) {
      const y=(i*193+s.elapsed*(10+i%5*6))%h;
      ctx.fillStyle=i%5===0?"#779da5":"#2a4250"; ctx.fillRect(i*79%w,y,1.4,i%5===0?2.4:1.4);
    }
    for (const r of s.rocks) {
      ctx.save(); ctx.translate(r.x,r.y); ctx.rotate(r.angle);
      shape(Array.from({length:9},(_,i) => {const a=i*2*Math.PI/9, v=r.r*(0.81+0.17*Math.sin(i*7));return [Math.cos(a)*v,Math.sin(a)*v];}),"#815f57","#e1aa8c");
      ctx.beginPath(); ctx.arc(-r.r*.2,-r.r*.2,r.r*.18,0,Math.PI*2); ctx.stroke(); ctx.restore();
    }
    ctx.fillStyle="#8ffff0"; for(const b of s.shots) ctx.fillRect(b.x-1.6,b.y-6,3.2,12);
    for(const b of s.bursts){ctx.strokeStyle='rgba(255,188,117,'+(1-b.t/.32)+')';ctx.beginPath();ctx.arc(b.x,b.y,4+b.t*83,0,Math.PI*2);ctx.stroke();}
    if (s.life && (!s.grace || Math.floor(s.grace*14)%2)) {
      ctx.save(); ctx.translate(s.x,s.y);
      shape([[-4,9],[0,24+Math.sin(s.elapsed*30)*4],[4,9]],"#ffa06a","#f6c795");
      shape([[0,-21],[17,14],[5,10],[0,13],[-5,10],[-17,14]],"#81bebc","#d0fff0");
      shape([[0,-11],[4,4],[0,7],[-4,4]],"#244a63","#6bd0d8"); ctx.restore();
    }
    $("score").textContent=String(s.score).padStart(6,"0"); $("best").textContent=String(best).padStart(6,"0");
    $("life").textContent="◆".repeat(s.life)+"◇".repeat(OrbitRules.shields-s.life);
  }
  const axis=(a,b,c,d)=>Number(keys.has(a)||keys.has(b))-Number(keys.has(c)||keys.has(d));
  function frame(now) {
    const prev=s.phase;
    const input=pointer?{x:(pointer.x-s.x)/10,y:(pointer.y-s.y)/10,fire:true}:{x:axis("ArrowRight","KeyD","ArrowLeft","KeyA"),y:axis("ArrowDown","KeyS","ArrowUp","KeyW"),fire:keys.has("Space")};
    OrbitGame.step(s,input,(now-last)/1000); last=now;
    if(s.score>best){best=s.score;try{localStorage.setItem("orbit-patrol.best",String(best));}catch{}}
    if(prev!==s.phase)controls();
    draw(); requestAnimationFrame(frame);
  }
  controls(); requestAnimationFrame(frame);
})();

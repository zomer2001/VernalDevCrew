const test = require("node:test");
const assert = require("node:assert/strict");
require("../contracts/rules.js");
require("../engine/game.js");
const {start, step, pause} = OrbitGame;
const tick = (s, input, frames=1) => { for(let i=0;i<frames;i++) step(s,input,1/60,()=>0.5); };
test("movement is bounded in all directions", () => {
  const s=start(); tick(s,{x:1,y:1},300); assert.equal(s.x,463); assert.equal(s.y,660);
  tick(s,{x:-1,y:-1},300); assert.equal(s.x,17); assert.equal(s.y,24);
});
test("held fire observes cooldown", () => {const s=start();tick(s,{fire:true},9);assert.equal(s.shots.length,1);tick(s,{fire:true},3);assert.equal(s.shots.length,2);});
const rock=(x,y)=>({x,y,r:15,speed:0,angle:0});
test("one shot destroys one asteroid and scores once", () => {const s=start();s.rocks=[rock(30,90),rock(30,90)];s.shots=[{x:30,y:97}];tick(s,{});assert.equal(s.score,100);assert.equal(s.rocks.length,1);});
test("shield collision, temporary invulnerability, game over and restart", () => {
  const s=start();s.rocks=[rock(s.x,s.y),rock(s.x,s.y)];tick(s,{});assert.equal(s.life,2);tick(s,{});assert.equal(s.life,2);
  s.grace=0;tick(s,{});assert.equal(s.life,1);s.grace=0;s.rocks=[rock(s.x,s.y)];tick(s,{});assert.equal(s.phase,"over");assert.equal(start().life,3);
});
test("pause freezes the entire simulation; resume advances time",()=>{const s=start();pause(s);const copy=JSON.stringify(s);tick(s,{x:1,fire:true},30);assert.equal(JSON.stringify(s),copy);pause(s);tick(s,{});assert.ok(s.elapsed>0);});

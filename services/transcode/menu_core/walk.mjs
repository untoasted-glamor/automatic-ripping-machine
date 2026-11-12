// src/walk-cli.ts
import { readFileSync } from "node:fs";

// src/dvdvm.ts
var SUBINS = {
  0: "LinkNoLink",
  1: "LinkTopC",
  2: "LinkNextC",
  3: "LinkPrevC",
  5: "LinkTopPG",
  6: "LinkNextPG",
  7: "LinkPrevPG",
  9: "LinkTopPGC",
  10: "LinkNextPGC",
  11: "LinkPrevPGC",
  12: "LinkGoUpPGC",
  13: "LinkTailPGC",
  16: "LinkRSM"
};
var MENU_ID = { title: 2, root: 3, subpicture: 4, audio: 5, angle: 6, chapter: 7 };
var link = (cmd, d1 = 0, d2 = 0, d3 = 0) => ({ cmd, d1, d2, d3 });
var Command = class {
  v;
  constructor(hex) {
    this.v = BigInt("0x" + hex);
  }
  bits(start, count) {
    return Number(this.v >> BigInt(start - count + 1) & (1n << BigInt(count)) - 1n);
  }
};
var cmdCache = /* @__PURE__ */ new Map();
function command(hex) {
  let c = cmdCache.get(hex);
  if (!c) cmdCache.set(hex, c = new Command(hex));
  return c;
}
var DvdVm = class {
  constructor(pack2, opts = {}) {
    this.pack = pack2;
    this.log = opts.log ?? (() => {
    });
    this.rnd = opts.rnd ?? Math.random;
    const lang = pack2.menu_lang || "en";
    this.sprm[0] = lang.charCodeAt(0) << 8 | lang.charCodeAt(1);
    this.sprm[1] = 15;
    this.sprm[2] = 62;
    this.sprm[3] = 1;
    this.sprm[4] = 1;
    this.sprm[5] = 1;
    this.sprm[7] = 1;
    this.sprm[8] = 1 << 10;
    this.sprm[12] = "U".charCodeAt(0) << 8 | "S".charCodeAt(0);
    this.sprm[13] = 15;
    this.sprm[14] = 256;
    this.sprm[15] = 31996;
    this.sprm[16] = this.sprm[0];
    this.sprm[18] = this.sprm[0];
    this.sprm[20] = 1;
  }
  pack;
  gprm = new Uint16Array(16);
  gprmCounter = new Uint8Array(16);
  sprm = new Uint16Array(24);
  stopped = false;
  state;
  rsm = null;
  log;
  rnd;
  steps = 0;
  // ------------------------------------------------------------------ public API
  start() {
    this.state = { domain: "FP", vtsN: 0, pgcN: 0, pgN: 1, cellN: 0, pgc: this.pack.vmg.fp };
    this.log("start: first play PGC");
    this.process(this.playPGC());
  }
  /** The cell to present now (the VM is waiting for cellEnded() or a button). */
  get cell() {
    if (this.stopped) return null;
    return this.state.pgc.cells[this.state.cellN - 1] ?? null;
  }
  get highlighted() {
    return this.sprm[8] >> 10;
  }
  /** Where this title cell's video lives, if the pack maps it. */
  get media() {
    if (this.state.domain !== "VTS") return null;
    return this.pack.media[`${this.state.vtsN}:${this.state.pgcN}`]?.cells[String(this.state.cellN)] ?? null;
  }
  /** The cell finished playing (and its still time is over): run its cell command or go on. */
  cellEnded() {
    if (this.stopped) return;
    this.process(this.playCellPost());
  }
  select(button) {
    this.sprm[8] = button << 10;
  }
  /** Run a menu button's command (dvdnav_button_activate -> vm_exec_cmd). */
  activate(button) {
    this.select(button.n);
    this.log(`button ${button.n}: ${button.txt}`);
    const r = this.evalCmds([button.cmd], [button.txt]);
    if (r) this.process(r);
  }
  /** The remote's menu keys (vm_jump_menu). */
  menuCall(menu) {
    const old = this.state.domain;
    if (old === "FP") return false;
    if (old === "VTS") this.setRSMinfo(0);
    this.state.domain = menu === "title" ? "VMGM" : "VTSM";
    if (this.setMenu(MENU_ID[menu])) {
      this.log(`menu key: ${menu}`);
      this.process(this.playPGC());
      return true;
    }
    this.state.domain = old;
    return false;
  }
  // ------------------------------------------------------------------ PGC tables
  pgcit(domain = this.state.domain) {
    const lang = this.pack.menu_lang;
    const pick = (m) => m[lang] ?? Object.values(m)[0] ?? null;
    if (domain === "VTS") return this.pack.vts[String(this.state.vtsN)]?.title_pgcs ?? null;
    if (domain === "VTSM") return this.pack.vts[String(this.state.vtsN)] ? pick(this.pack.vts[String(this.state.vtsN)].menus) : null;
    return pick(this.pack.vmg.menus);
  }
  setPGCN(pgcN) {
    const t = this.pgcit();
    if (!t || pgcN < 1 || pgcN > t.length) return false;
    this.state.pgc = t[pgcN - 1];
    this.state.pgcN = pgcN;
    this.state.pgN = 1;
    if (this.state.domain === "VTS") this.sprm[6] = pgcN;
    return true;
  }
  setMenu(id) {
    const t = this.pgcit();
    if (!t) return false;
    const i = t.findIndex((p) => p.entry !== null && MENU_ID[p.entry] === id);
    return i >= 0 && this.setPGCN(i + 1);
  }
  getTT(vtsN, ttn) {
    return this.pack.vmg.titles.findIndex((t) => t.vts === vtsN && t.ttn === ttn) + 1;
  }
  setVTS_PTT(vtsN, ttn, part) {
    this.state.domain = "VTS";
    this.state.vtsN = vtsN;
    const v = this.pack.vts[String(vtsN)];
    if (!v || ttn < 1 || ttn > v.ptt.length || part < 1 || part > v.ptt[ttn - 1].length) return false;
    const [pgcN, pgN] = v.ptt[ttn - 1][part - 1];
    this.sprm[6] = pgcN;
    this.sprm[7] = part;
    this.sprm[4] = this.getTT(vtsN, ttn);
    if (this.sprm[4] === 0) return false;
    this.sprm[5] = ttn;
    const ok = this.setPGCN(pgcN);
    this.state.pgN = pgN;
    return ok;
  }
  setTT(tt) {
    const t = this.pack.vmg.titles[tt - 1];
    return !!t && this.setVTS_PTT(t.vts, t.ttn, 1);
  }
  setPGN() {
    const p = this.state.pgc;
    let n = 0;
    while (n < p.program_map.length && this.state.cellN >= p.program_map[n]) n++;
    if (n === p.program_map.length && this.state.cellN > p.cells.length) return false;
    this.state.pgN = n;
    if (this.state.domain === "VTS") {
      const ptts = this.pack.vts[String(this.state.vtsN)]?.ptt[this.sprm[5] - 1] ?? [];
      let part = 0;
      ptts.forEach(([pgcn, pgn], i) => {
        if (pgcn === this.state.pgcN && pgn <= this.state.pgN) part = i + 1;
      });
      if (part) this.sprm[7] = part;
    }
    return true;
  }
  setRSMinfo(cellN) {
    this.rsm = {
      vtsN: this.state.vtsN,
      pgcN: this.state.pgcN,
      cellN: cellN || this.state.cellN,
      regs: Array.from(this.sprm.slice(4, 9))
    };
  }
  // ------------------------------------------------------------------ play state machine (vm.c)
  playPGC() {
    this.state.pgN = 1;
    this.state.cellN = 0;
    this.log(`PGC ${this.where()}${this.state.pgc.entry ? ` (${this.state.pgc.entry} menu)` : ""}`);
    const r = this.evalCmds(this.state.pgc.pre, this.state.pgc.txt.pre);
    return r ?? this.playPG();
  }
  playPGC_PG(pgN) {
    this.state.pgN = pgN;
    this.state.cellN = 0;
    this.log(`PGC ${this.where()} from program ${pgN}`);
    const r = this.evalCmds(this.state.pgc.pre, this.state.pgc.txt.pre);
    return r ?? this.playPG();
  }
  playPGCPost() {
    const r = this.evalCmds(this.state.pgc.post, this.state.pgc.txt.post);
    if (r) return r;
    if (!this.setPGCN(this.state.pgc.next)) return link("Exit");
    return this.playPGC();
  }
  playPG() {
    const p = this.state.pgc;
    if (this.state.pgN > p.program_map.length) return this.playPGCPost();
    this.state.cellN = p.program_map[this.state.pgN - 1];
    return this.playCell();
  }
  playCell() {
    if (this.state.cellN > this.state.pgc.cells.length || this.state.cellN < 1) return this.playPGCPost();
    if (!this.setPGN()) return this.playPGCPost();
    return link("PlayThis");
  }
  playCellPost() {
    const cell = this.state.pgc.cells[this.state.cellN - 1];
    if (cell && cell.cmd) {
      const i = cell.cmd - 1;
      if (i < this.state.pgc.cellcmds.length) {
        const r = this.evalCmds([this.state.pgc.cellcmds[i]], [this.state.pgc.txt.cellcmds[i]]);
        if (r) return r;
      }
    }
    this.state.cellN++;
    if (!this.setPGN()) return this.playPGCPost();
    return this.playCell();
  }
  process(l) {
    let guard = 0;
    while (l.cmd !== "PlayThis") {
      if (++guard > 5e3) {
        this.log("process: too many links without a cell to play; stopping");
        this.stopped = true;
        return;
      }
      const s = this.state;
      switch (l.cmd) {
        case "LinkNoLink":
          if (l.d1) this.sprm[8] = l.d1 << 10;
          return;
        case "LinkTopC":
          if (l.d1) this.sprm[8] = l.d1 << 10;
          l = this.playCell();
          break;
        case "LinkNextC":
          if (l.d1) this.sprm[8] = l.d1 << 10;
          s.cellN++;
          l = this.playCell();
          break;
        case "LinkPrevC":
          if (l.d1) this.sprm[8] = l.d1 << 10;
          s.cellN--;
          l = this.playCell();
          break;
        case "LinkTopPG":
          if (l.d1) this.sprm[8] = l.d1 << 10;
          l = this.playPG();
          break;
        case "LinkNextPG":
          if (l.d1) this.sprm[8] = l.d1 << 10;
          s.pgN++;
          l = this.playPG();
          break;
        case "LinkPrevPG":
          if (l.d1) this.sprm[8] = l.d1 << 10;
          s.pgN--;
          l = this.playPG();
          break;
        case "LinkTopPGC":
          if (l.d1) this.sprm[8] = l.d1 << 10;
          l = this.playPGC();
          break;
        case "LinkNextPGC":
        case "LinkPrevPGC":
        case "LinkGoUpPGC": {
          if (l.d1) this.sprm[8] = l.d1 << 10;
          const n = l.cmd === "LinkNextPGC" ? s.pgc.next : l.cmd === "LinkPrevPGC" ? s.pgc.prev : s.pgc.goup;
          l = n && this.setPGCN(n) ? this.playPGC() : link("Exit");
          break;
        }
        case "LinkTailPGC":
          if (l.d1) this.sprm[8] = l.d1 << 10;
          l = this.playPGCPost();
          break;
        case "LinkRSM": {
          if (!this.rsm) {
            this.log("resume without resume info");
            l = link("Exit");
            break;
          }
          s.domain = "VTS";
          s.vtsN = this.rsm.vtsN;
          this.setPGCN(this.rsm.pgcN);
          this.rsm.regs.forEach((v, i) => this.sprm[4 + i] = v);
          if (l.d1) this.sprm[8] = l.d1 << 10;
          this.log(`resume: ${this.where()} cell ${this.rsm.cellN}`);
          if (this.rsm.cellN === 0) {
            s.pgN = 1;
            l = this.playPG();
          } else {
            s.cellN = this.rsm.cellN;
            l = this.setPGN() ? link("PlayThis") : link("LinkTailPGC");
          }
          break;
        }
        case "LinkPGCN":
          l = this.setPGCN(l.d1) ? this.playPGC() : link("Exit");
          break;
        case "LinkPTTN":
          if (l.d2) this.sprm[8] = l.d2 << 10;
          l = this.setVTS_PTT(s.vtsN, this.sprm[5], l.d1) ? this.playPG() : link("Exit");
          break;
        case "LinkPGN":
          if (l.d2) this.sprm[8] = l.d2 << 10;
          s.pgN = l.d1;
          l = this.playPG();
          break;
        case "LinkCN":
          if (l.d2) this.sprm[8] = l.d2 << 10;
          s.cellN = l.d1;
          l = this.playCell();
          break;
        case "Exit":
          this.log("exit: playback stopped");
          this.stopped = true;
          return;
        case "JumpTT":
          l = this.setTT(l.d1) ? this.playPGC() : link("Exit");
          break;
        case "JumpVTS_TT":
          l = this.setVTS_PTT(s.vtsN, l.d1, 1) ? this.playPGC() : link("Exit");
          break;
        case "JumpVTS_PTT":
          l = this.setVTS_PTT(s.vtsN, l.d1, l.d2) ? this.playPGC_PG(s.pgN) : link("Exit");
          break;
        case "JumpSS_FP":
          s.domain = "FP";
          s.pgc = this.pack.vmg.fp;
          s.pgcN = 0;
          l = this.playPGC();
          break;
        case "JumpSS_VMGM_MENU":
          s.domain = "VMGM";
          l = this.setMenu(l.d1) ? this.playPGC() : link("Exit");
          break;
        case "JumpSS_VTSM":
          if (l.d1) {
            s.vtsN = l.d1;
            s.domain = "VTSM";
            this.sprm[5] = l.d2;
            this.sprm[4] = this.getTT(s.vtsN, l.d2);
          }
          l = this.setMenu(l.d3) ? this.playPGC() : link("Exit");
          break;
        case "JumpSS_VMGM_PGC":
          s.domain = "VMGM";
          l = this.setPGCN(l.d1) ? this.playPGC() : link("Exit");
          break;
        case "CallSS_FP":
          this.setRSMinfo(l.d1);
          s.domain = "FP";
          s.pgc = this.pack.vmg.fp;
          s.pgcN = 0;
          l = this.playPGC();
          break;
        case "CallSS_VMGM_MENU":
          this.setRSMinfo(l.d2);
          s.domain = "VMGM";
          l = this.setMenu(l.d1) ? this.playPGC() : link("Exit");
          break;
        case "CallSS_VTSM":
          this.setRSMinfo(l.d2);
          s.domain = "VTSM";
          l = this.setMenu(l.d1) ? this.playPGC() : link("Exit");
          break;
        case "CallSS_VMGM_PGC":
          this.setRSMinfo(l.d2);
          s.domain = "VMGM";
          l = this.setPGCN(l.d1) ? this.playPGC() : link("Exit");
          break;
      }
    }
    this.log(`play ${this.where()} cell ${this.state.cellN}`);
  }
  where() {
    const s = this.state;
    if (s.domain === "FP") return "FP";
    if (s.domain === "VMGM") return `VMGM pgc ${s.pgcN}`;
    if (s.domain === "VTSM") return `VTSM${s.vtsN} pgc ${s.pgcN}`;
    return `VTS${s.vtsN} pgc ${s.pgcN} (title ${this.sprm[4]}, chapter ${this.sprm[7]})`;
  }
  // ------------------------------------------------------------------ command evaluation (decoder.c)
  /** Run a command list; a Link result means "jump", null means it fell through (or hit Break). */
  evalCmds(cmds, txt) {
    let i = 0;
    let total = 0;
    while (i < cmds.length && total < 1e5) {
      if (++this.steps % 1e5 === 0) this.log(`(${this.steps} commands run)`);
      const out = { link: null };
      const line = this.evalCmd(command(cmds[i]), out);
      if (line < 0) {
        this.log(`  ${txt[i] ?? cmds[i]}  -> ${out.link.cmd}${out.link.d1 ? " " + out.link.d1 : ""}`);
        return out.link;
      }
      i = line > 0 ? line - 1 : i + 1;
      total++;
    }
    return null;
  }
  getG(r) {
    return this.gprm[r & 15];
  }
  setG(r, v) {
    this.gprm[r & 15] = v & 65535;
  }
  reg(r) {
    if (r & 128) return this.sprm[r & 31] ?? 0;
    return this.getG(r & 15);
  }
  regOrData(c, imm, start) {
    return imm ? c.bits(start, 16) : this.reg(c.bits(start - 8, 8));
  }
  regOrData2(c, imm, start) {
    return imm ? c.bits(start - 1, 7) : this.getG(c.bits(start - 4, 4));
  }
  compare(op, a, b) {
    switch (op) {
      case 1:
        return (a & b) !== 0;
      case 2:
        return a === b;
      case 3:
        return a !== b;
      case 4:
        return a >= b;
      case 5:
        return a > b;
      case 6:
        return a <= b;
      case 7:
        return a < b;
    }
    return false;
  }
  if1(c) {
    const op = c.bits(54, 3);
    return op ? this.compare(op, this.reg(c.bits(39, 8)), this.regOrData(c, c.bits(55, 1), 31)) : true;
  }
  if2(c) {
    const op = c.bits(54, 3);
    return op ? this.compare(op, this.reg(c.bits(15, 8)), this.reg(c.bits(7, 8))) : true;
  }
  if3(c) {
    const op = c.bits(54, 3);
    return op ? this.compare(op, this.reg(c.bits(43, 4)), this.regOrData(c, c.bits(55, 1), 15)) : true;
  }
  if4(c) {
    const op = c.bits(54, 3);
    return op ? this.compare(op, this.reg(c.bits(51, 4)), this.regOrData(c, c.bits(55, 1), 31)) : true;
  }
  if5(c) {
    const op = c.bits(54, 3);
    if (!op) return true;
    if (c.bits(60, 1)) return this.compare(op, this.reg(c.bits(31, 8)), this.reg(c.bits(23, 8)));
    return this.compare(op, this.reg(c.bits(39, 8)), this.reg(c.bits(31, 8)));
  }
  setOp(op, r, r2, data) {
    const cur = this.getG(r);
    switch (op) {
      case 1:
        this.setG(r, data);
        break;
      case 2:
        this.setG(r2, this.gprm[r]);
        this.setG(r, data);
        break;
      case 3:
        this.setG(r, Math.min(cur + data, 65535));
        break;
      case 4:
        this.setG(r, Math.max(cur - data, 0));
        break;
      case 5:
        this.setG(r, Math.min(cur * data, 65535));
        break;
      case 6:
        this.setG(r, data ? Math.floor(cur / data) : 65535);
        break;
      case 7:
        this.setG(r, data ? cur % data : 65535);
        break;
      case 8:
        this.setG(r, 1 + Math.floor(data * this.rnd()));
        break;
      case 9:
        this.setG(r, cur & data);
        break;
      case 10:
        this.setG(r, cur | data);
        break;
      case 11:
        this.setG(r, cur ^ data);
        break;
    }
  }
  set1(c, cond) {
    const data = this.regOrData(c, c.bits(60, 1), 31);
    if (cond) this.setOp(c.bits(59, 4), c.bits(35, 4), c.bits(19, 4), data);
  }
  set2(c, cond) {
    const data = this.regOrData(c, c.bits(60, 1), 47);
    if (cond) this.setOp(c.bits(59, 4), c.bits(51, 4), c.bits(35, 4), data);
  }
  linkSubins(c, cond, out) {
    const op = c.bits(4, 5);
    const cmd = SUBINS[op];
    if (!cmd) return false;
    out.link = link(cmd, c.bits(15, 6));
    return cond;
  }
  linkIns(c, cond, out) {
    switch (c.bits(51, 4)) {
      case 1:
        return this.linkSubins(c, cond, out);
      case 4:
        out.link = link("LinkPGCN", c.bits(14, 15));
        return cond;
      case 5:
        out.link = link("LinkPTTN", c.bits(9, 10), c.bits(15, 6));
        return cond;
      case 6:
        out.link = link("LinkPGN", c.bits(6, 7), c.bits(15, 6));
        return cond;
      case 7:
        out.link = link("LinkCN", c.bits(7, 8), c.bits(15, 6));
        return cond;
    }
    return false;
  }
  jumpIns(c, cond, out) {
    switch (c.bits(51, 4)) {
      case 1:
        out.link = link("Exit");
        return cond;
      case 2:
        out.link = link("JumpTT", c.bits(22, 7));
        return cond;
      case 3:
        out.link = link("JumpVTS_TT", c.bits(22, 7));
        return cond;
      case 5:
        out.link = link("JumpVTS_PTT", c.bits(22, 7), c.bits(41, 10));
        return cond;
      case 6:
        switch (c.bits(23, 2)) {
          case 0:
            out.link = link("JumpSS_FP");
            return cond;
          case 1:
            out.link = link("JumpSS_VMGM_MENU", c.bits(19, 4));
            return cond;
          case 2:
            out.link = link("JumpSS_VTSM", c.bits(31, 8), c.bits(39, 8), c.bits(19, 4));
            return cond;
          case 3:
            out.link = link("JumpSS_VMGM_PGC", c.bits(46, 15));
            return cond;
        }
        break;
      case 8:
        switch (c.bits(23, 2)) {
          case 0:
            out.link = link("CallSS_FP", c.bits(31, 8));
            return cond;
          case 1:
            out.link = link("CallSS_VMGM_MENU", c.bits(19, 4), c.bits(31, 8));
            return cond;
          case 2:
            out.link = link("CallSS_VTSM", c.bits(19, 4), c.bits(31, 8));
            return cond;
          case 3:
            out.link = link("CallSS_VMGM_PGC", c.bits(46, 15), c.bits(31, 8));
            return cond;
        }
        break;
    }
    return false;
  }
  systemSet(c, cond, out) {
    switch (c.bits(59, 4)) {
      case 1:
        for (let i = 1; i <= 3; i++) {
          if (c.bits(63 - (2 + i) * 8, 1)) {
            const d = this.regOrData2(c, c.bits(60, 1), 47 - i * 8);
            if (cond) this.sprm[i] = d;
          }
        }
        break;
      case 2: {
        const d = this.regOrData(c, c.bits(60, 1), 47);
        const d2 = c.bits(15, 16);
        if (cond) {
          this.sprm[9] = d;
          this.sprm[10] = d2;
          this.log(`  (navigation timer set to ${d} s; the player does not run it)`);
        }
        break;
      }
      case 3: {
        const d = this.regOrData(c, c.bits(60, 1), 47);
        const r = c.bits(19, 4);
        this.gprmCounter[r] = c.bits(23, 1);
        if (cond) this.setG(r, d);
        break;
      }
      case 6: {
        const d = this.regOrData(c, c.bits(60, 1), 31);
        if (cond) this.sprm[8] = d;
        break;
      }
    }
    return c.bits(51, 4) ? this.linkIns(c, cond, out) : false;
  }
  special(c, cond) {
    switch (c.bits(51, 4)) {
      case 0:
        return 0;
      case 1:
        return cond ? c.bits(7, 8) : 0;
      case 2:
        return cond ? 256 : 0;
      case 3:
        if (cond) this.sprm[13] = c.bits(11, 4);
        return cond ? c.bits(7, 8) : 0;
    }
    return 0;
  }
  /** Returns -1 for a link (in out.link), >0 for a goto line, 0 to go on. */
  evalCmd(c, out) {
    let cond;
    switch (c.bits(63, 3)) {
      case 0:
        cond = this.if1(c);
        return this.special(c, cond);
      case 1:
        if (c.bits(60, 1)) {
          cond = this.if2(c);
          return this.jumpIns(c, cond, out) ? -1 : 0;
        }
        cond = this.if1(c);
        return this.linkIns(c, cond, out) ? -1 : 0;
      case 2:
        cond = this.if2(c);
        return this.systemSet(c, cond, out) ? -1 : 0;
      case 3:
        cond = this.if3(c);
        this.set1(c, cond);
        return c.bits(51, 4) && this.linkIns(c, cond, out) ? -1 : 0;
      case 4:
        this.set2(c, true);
        cond = this.if4(c);
        return this.linkSubins(c, cond, out) ? -1 : 0;
      case 5:
        cond = this.if5(c);
        this.set2(c, cond);
        return this.linkSubins(c, cond, out) ? -1 : 0;
      case 6:
        cond = this.if5(c);
        this.set2(c, cond);
        return this.linkSubins(c, true, out) ? -1 : 0;
    }
    return 0;
  }
};

// src/hdmvvm.ts
var TICKS = 45e3;
var PSR_INIT = {
  0: 1,
  1: 255,
  2: 268374015,
  3: 1,
  4: 65535,
  5: 65535,
  6: 0,
  7: 0,
  8: 0,
  9: 0,
  10: 65535,
  11: 0,
  12: 255,
  13: 255,
  14: 65535,
  15: 65535,
  16: 6647399,
  17: 6647399,
  18: 6647399,
  19: 21843,
  20: 1,
  21: 65535,
  29: 3,
  30: 131071,
  31: 524800,
  36: 65535,
  37: 65535,
  42: 65535,
  44: 255
};
var STEP_LIMIT = 5e4;
var NONE = 65535;
var AUTO_DEPTH = 64;
var langCode = (lang3) => lang3.length === 3 ? lang3.charCodeAt(0) << 16 | lang3.charCodeAt(1) << 8 | lang3.charCodeAt(2) : 16777215;
function decode(hex) {
  const n = (o) => parseInt(hex.slice(o * 2, o * 2 + 8), 16) >>> 0;
  const w = n(0);
  return {
    opCnt: w >>> 29,
    grp: w >>> 27 & 3,
    sub: w >>> 24 & 7,
    imm1: (w >>> 23 & 1) === 1,
    imm2: (w >>> 22 & 1) === 1,
    br: w >>> 16 & 15,
    cmp: w >>> 8 & 15,
    set: w & 31,
    dst: n(4),
    src: n(8)
  };
}
var compare = (op, a, b) => {
  switch (op) {
    case 1:
      return (a & b) >>> 0 === b;
    case 2:
      return a === b;
    case 3:
      return a !== b;
    case 4:
      return a >= b;
    case 5:
      return a > b;
    case 6:
      return a <= b;
    case 7:
      return a < b;
    default:
      return false;
  }
};
var HdmvVm = class {
  constructor(pack2, opts = {}) {
    this.pack = pack2;
    this.log = opts.log ?? (() => {
    });
    this.unsupportedCb = opts.unsupported ?? (() => {
    });
    this.rnd = opts.rnd ?? Math.random;
    for (const [k, v] of Object.entries(PSR_INIT)) this.psr[Number(k)] = v;
    if (opts.audioLang) this.psr[16] = this.psr[18] = langCode(opts.audioLang);
    if (opts.subtitleLang) this.psr[17] = langCode(opts.subtitleLang);
    for (const [k, v] of Object.entries(pack2.psr ?? {})) this.psr[Number(k)] = v >>> 0;
  }
  pack;
  gpr = new Uint32Array(4096);
  psr = new Uint32Array(128);
  status = { kind: "stopped", why: "not started" };
  log;
  unsupportedCb;
  seen = /* @__PURE__ */ new Set();
  rnd;
  steps = 0;
  /** The movie object waiting for its playlist to end, and where it goes on. */
  suspended = null;
  stack = [];
  // ------------------------------------------------------------------------------------------ public API
  /** Run first play until it shows a menu, hands off or stops. */
  start() {
    this.log("start: first play");
    this.steps = 0;
    return this.settle(this.runObject(this.pack.index.first_play, 0));
  }
  /** The remote's top menu key: the index's top menu object, unless the playing object masks menu calls. */
  topMenu() {
    if (this.suspended && this.pack.objects[String(this.suspended.object)]?.menu_call_mask) {
      this.log("top menu: masked by the playing object");
      return this.status;
    }
    this.log("top menu");
    this.steps = 0;
    this.stack.length = 0;
    this.psr[4] = 0;
    return this.settle(this.runObject(this.pack.index.top_menu, 0));
  }
  /**
   * The handed-off library item finished (or the viewer stopped it and asked for the menu back): the playlist that
   * started it has ended, so the movie object that played it goes on, as a disc's does.
   */
  ended() {
    this.log("playlist ended");
    this.steps = 0;
    if (!this.suspended) return this.status = { kind: "stopped", why: "nothing to go on with" };
    return this.resumeSuspended();
  }
  /** The menu's buttons as they are drawn now: the enabled button of each group, in its state. */
  shownButtons() {
    if (this.status.kind !== "menu") return [];
    const m = this.status.menu;
    const out = [];
    m.page.bogs.forEach((g, i) => {
      const id = m.enabled[i];
      const b = id == null ? void 0 : g.buttons.find((x) => x.id === id);
      if (!b) return;
      const state = b.id === m.selected ? "selected" : "normal";
      const image = (state === "selected" ? b.images.selected[0] : void 0) ?? b.images.normal[0] ?? null;
      out.push({ button: b, state, image });
    });
    return out;
  }
  /** Arrow keys: the selected button's neighbour, if it is shown; an auto-action button runs at once. */
  move(dir) {
    if (this.status.kind !== "menu") return this.status;
    const m = this.status.menu;
    const cur = this.button(m, m.selected);
    if (!cur) return this.status;
    const to = cur.nav[dir];
    if (to === cur.id || !this.isEnabled(m, to)) return this.status;
    this.selectButton(m, to);
    this.steps = 0;
    return this.render(m, 0);
  }
  /** Select a shown button (a pointer over it); an auto-action button runs at once. */
  select(id) {
    if (this.status.kind !== "menu" || id == null) return this.status;
    const m = this.status.menu;
    if (!this.isEnabled(m, id) || id === m.selected) return this.status;
    this.selectButton(m, id);
    this.steps = 0;
    return this.render(m, 0);
  }
  /** OK / Enter: run the selected button's commands. */
  activate() {
    if (this.status.kind !== "menu") return this.status;
    const m = this.status.menu;
    const b = this.button(m, m.selected);
    if (!b) return this.status;
    this.steps = 0;
    return this.press(m, b, 0);
  }
  /** A number key selects the shown button with that number (libbluray: it runs only if it is auto-action). */
  number(n) {
    if (this.status.kind !== "menu") return this.status;
    const hit = this.shownButtons().find((s) => s.button.numeric === n);
    return hit ? this.select(hit.button.id) : this.status;
  }
  /** Where a play of `playlist` from `start` ticks lands: its media cell and the cells after it, or null. */
  cellAt(playlist, start) {
    const cells = this.pack.media[playlist]?.cells;
    if (!cells) return null;
    const keys = Object.keys(cells).map(Number).sort((a, b) => a - b);
    let hit = -1;
    for (let i = 0; i < keys.length; i++) if (keys[i] <= start) hit = i;
    if (hit < 0) return null;
    const cell = cells[String(keys[hit])];
    if (start >= keys[hit] + Math.round(cell.dur * TICKS)) return null;
    return { key: keys[hit], cell, queue: keys.slice(hit + 1).map((k) => cells[String(k)]) };
  }
  // ------------------------------------------------------------------------------------------ playlists
  /** Ticks from the playlist's start at which play item `item` starts (repeats counted). */
  itemStart(pl, item) {
    let n = 0;
    let t = 0;
    for (const it of pl.items) {
      const reps = it.repeat ?? 1;
      for (let r = 0; r < reps; r++, n++) {
        if (n === item) return t;
        t += it.out - it.in;
      }
    }
    return t;
  }
  play(ev) {
    const name = String(ev.playlist).padStart(5, "0");
    const pl = this.pack.playlists[name];
    this.psr[6] = ev.playlist;
    if (!pl) {
      this.log(`play_pl ${name}: not in the pack; plays through`);
      return null;
    }
    let start = 0;
    if (ev.mark != null) {
      const mk = pl.marks[ev.mark];
      start = mk?.at ?? 0;
      if (!mk) this.log(`play_pl ${name} mark ${ev.mark}: no such mark, from the start`);
    } else if (ev.item != null) start = this.itemStart(pl, ev.item);
    return this.decide(name, pl, start);
  }
  /** play_pl's three outcomes, in order: a handoff, a menu, playing through (null). */
  decide(name, pl, start) {
    const hit = this.cellAt(name, start);
    if (hit) {
      const handoff = {
        playlist: name,
        start,
        cell: hit.cell,
        offset: hit.cell.offset + (start - hit.key) / TICKS,
        queue: hit.queue,
        audio: this.audioStream(pl),
        pg: this.pgStream(pl),
        audioSet: pl.streams.audio.some((a) => a.n === (this.psr[1] & 255)),
        pgSet: pl.streams.pg.some((p) => p.n === (this.psr[2] & 4095))
      };
      this.log(`play_pl ${name} @${(start / TICKS).toFixed(2)}s: hands off ${describeCell(hit.cell)}` + (hit.queue.length ? `, then ${hit.queue.length} more` : ""));
      this.status = { kind: "handoff", handoff };
      return this.status;
    }
    const ig = this.psr[0] & 255 || 1;
    const key = pl.menus[String(ig)] ?? pl.menus["1"];
    const menu = key ? this.pack.menus[key] : void 0;
    if (menu && menu.model === "always_on" && menu.pages?.length) {
      this.log(`play_pl ${name}: shows menu ${key}`);
      return this.showMenu(name, key, menu);
    }
    if (menu?.model === "popup") this.unsupported(`pop-up menu ${key}`);
    const secs = pl.items.reduce((s, it) => s + (it.out - it.in) * (it.repeat ?? 1) / TICKS, 0);
    this.log(`play_pl ${name} (${secs.toFixed(1)}s): no media and no menu, plays through`);
    return null;
  }
  /** PSR 1's audio stream; when it names none in this playlist, the viewer's language (PSR 16), else the first. */
  audioStream(pl) {
    const n = this.psr[1] & 255;
    const all = pl.streams.audio;
    return all.find((a) => a.n === n) ?? all.find((a) => langCode(a.lang3) === this.psr[16]) ?? all[0] ?? null;
  }
  /** PSR 2's subtitles when its display flag is set (else off); an unknown number falls back to PSR 17's language. */
  pgStream(pl) {
    if (!(this.psr[2] & 2147483648)) return null;
    const n = this.psr[2] & 4095;
    const all = pl.streams.pg;
    return all.find((p) => p.n === n) ?? all.find((p) => langCode(p.lang3) === this.psr[17]) ?? all[0] ?? null;
  }
  // ------------------------------------------------------------------------------------------ menus
  // After libbluray's graphics_controller.c: _select_page, _find_selected_button_id, _render_page's auto-action,
  // _set_button_page, _enable_button and _update_selected_button.
  showMenu(playlist, key, menu) {
    if (menu.timeout?.user || menu.timeout?.selection) this.unsupported("menu timeouts");
    const page = menu.pages.find((p) => p.id === 0) ?? menu.pages[0];
    const m = { playlist, key, menu, page, selected: null, enabled: [] };
    this.status = { kind: "menu", menu: m };
    this.selectPage(m, page);
    return this.render(m, 0);
  }
  /** A new page (PSR 11): each group shows its default button, and a button is selected (PSR 10). */
  selectPage(m, page) {
    m.page = page;
    this.psr[11] = page.id;
    m.enabled = page.bogs.map((g) => g.default != null && g.buttons.some((b) => b.id === g.default) ? g.default : null);
    if (page.effects) this.unsupported("page effects");
    if (page.default_activated != null) this.unsupported("default-activated buttons");
    for (const g of page.bogs)
      for (const b of g.buttons) {
        if (b.sounds) this.unsupported("button sounds");
        if (b.images.normal.length > 1 || b.images.selected.length > 1) this.unsupported("animated buttons");
      }
    this.selectButton(m, this.findSelected(m));
  }
  /** The page's default button, else PSR 10, else the first shown button (BD spec 5.9.8.3, as libbluray runs it). */
  findSelected(m) {
    const d = m.page.default_selected;
    if (d != null && this.isEnabled(m, d)) return d;
    if (this.isEnabled(m, this.psr[10])) return this.psr[10];
    return m.enabled.find((id) => id != null) ?? null;
  }
  selectButton(m, id) {
    m.selected = id;
    this.psr[10] = id ?? NONE;
    m.autoDone = false;
  }
  /** Drawing the page runs the selected button if it is auto-action, once per selection. */
  render(m, depth) {
    const b = this.button(m, m.selected);
    if (!b?.auto || m.autoDone) return this.status;
    m.autoDone = true;
    if (depth >= AUTO_DEPTH) {
      this.log("auto-action buttons loop; stopped following them");
      return this.status;
    }
    return this.press(m, b, depth + 1);
  }
  button(m, id) {
    if (id == null) return void 0;
    for (const g of m.page.bogs) {
      const b = g.buttons.find((x) => x.id === id);
      if (b) return b;
    }
    return void 0;
  }
  group(m, id) {
    return m.page.bogs.findIndex((g) => g.buttons.some((b) => b.id === id));
  }
  isEnabled(m, id) {
    if (id == null || id === NONE) return false;
    return m.enabled.includes(id);
  }
  /** enable_button / disable_button from a button's commands. */
  setEnabled(m, id, on) {
    const g = this.group(m, id);
    if (g < 0) return;
    if (on) {
      if (m.enabled[g] != null && m.enabled[g] === m.selected) m.pendingSelect = id;
      m.enabled[g] = id;
    } else {
      if (m.enabled[g] === id) m.enabled[g] = null;
      if (m.selected === id) {
        m.selected = null;
        this.psr[10] = NONE;
      }
    }
  }
  /** set_button_page from a button's commands (they stop there). */
  setButtonPage(m, page, button) {
    if (page == null && button == null) return;
    if (page != null) {
      if (page === m.page.id) return;
      const p = m.menu.pages.find((x) => x.id === page);
      if (!p) return;
      this.selectPage(m, p);
    } else if (button != null && (this.group(m, button) < 0 || button === m.selected)) return;
    if (button != null) {
      const g = this.group(m, button);
      if (g < 0) return;
      m.enabled[g] = button;
      this.selectButton(m, button);
    }
  }
  /** Run a button's commands and act on what they come to. */
  press(m, b, depth) {
    this.log(`button ${b.id}` + (b.txt?.length ? `: ${b.txt.join("; ")}` : ""));
    const ev = this.run(b.cmds, null, 0);
    if (this.status.kind !== "menu" || this.status.menu !== m) return this.status;
    switch (ev.kind) {
      case "page":
        this.setButtonPage(m, ev.page, ev.button);
        break;
      case "play": {
        const s = this.play(ev);
        if (s) return s;
        return this.resumeSuspended();
      }
      case "link": {
        const pl = this.pack.playlists[m.playlist];
        const start = ev.mark != null ? pl.marks[ev.mark]?.at ?? 0 : this.itemStart(pl, ev.item ?? 0);
        if (this.cellAt(m.playlist, start)) return this.decide(m.playlist, pl, start) ?? this.status;
        break;
      }
      case "jump":
        if (ev.call && this.suspended) this.stack.push(this.suspended);
        else if (!ev.call) this.stack.length = 0;
        return this.settle(this.runObject(ev.object, 0));
      case "end":
        if (ev.why === "resume") {
          const back = this.stack.pop();
          if (back) return this.settle(this.runObject(back.object, back.pc));
        }
        break;
    }
    if (m.pendingSelect != null) {
      this.selectButton(m, m.pendingSelect);
      m.pendingSelect = void 0;
    } else if (m.selected == null) this.selectButton(m, this.findSelected(m));
    return this.render(m, depth);
  }
  resumeSuspended() {
    const s = this.suspended;
    if (!s) return this.status;
    return this.settle(this.runObject(s.object, s.pc));
  }
  // ------------------------------------------------------------------------------------------ movie objects
  /**
   * Run movie objects from `object` at `pc` until a play_pl shows a menu or hands off, or they stop. Playlists that
   * play through return to the object.
   */
  runObject(object, pc) {
    for (; ; ) {
      const obj = this.pack.objects[String(object)];
      if (!obj) return { kind: "end", why: `no movie object ${object}` };
      const ev = this.run(obj.cmds, object, pc);
      if (ev.kind === "play" && "next" in ev) {
        const at = ev;
        this.suspended = { object, pc: at.next };
        const s = this.play(ev);
        if (s) return s;
        pc = at.next;
        continue;
      }
      if (ev.kind === "jump") {
        if (ev.call) this.stack.push({ object, pc: ev.next });
        else this.stack.length = 0;
        object = ev.object;
        pc = 0;
        continue;
      }
      if (ev.kind === "end" && ev.why === "resume") {
        const back = this.stack.pop();
        if (!back) return { kind: "end", why: "resume with nothing to resume" };
        object = back.object;
        pc = back.pc;
        continue;
      }
      return ev;
    }
  }
  settle(r) {
    if ("kind" in r && (r.kind === "menu" || r.kind === "handoff" || r.kind === "stopped")) return r;
    const ev = r;
    if (ev.kind === "end") {
      this.log(`stopped: ${ev.why}`);
      this.status = { kind: "stopped", why: ev.why };
    }
    return this.status;
  }
  titleObject(n) {
    if (n === 0) {
      this.psr[4] = 0;
      return this.pack.index.top_menu;
    }
    const t = this.pack.index.titles.find((x) => x.n === n);
    if (!t) return null;
    if ("bdj" in t) {
      this.unsupported(`BD-J title ${n}`);
      return null;
    }
    this.psr[4] = n;
    return t.object;
  }
  val(v, imm) {
    if (imm) return v >>> 0;
    return v & 2147483648 ? this.psr[v & 127] : this.gpr[v & 4095];
  }
  put(v, x) {
    if (!(v & 2147483648)) this.gpr[v & 4095] = x >>> 0;
  }
  /**
   * Run commands from `pc`. `object` is the movie object they belong to, or null for a button's commands, which
   * return a page change instead of applying it.
   */
  run(hexes, object, pc) {
    const cmds = hexes.map(decode);
    let skip = false;
    while (pc < cmds.length) {
      if (++this.steps > STEP_LIMIT) return { kind: "end", why: "step limit" };
      const c = cmds[pc++];
      if (skip) {
        skip = false;
        continue;
      }
      const a = c.opCnt >= 1 ? this.val(c.dst, c.imm1) : 0;
      const b = c.opCnt >= 2 ? this.val(c.src, c.imm2) : 0;
      if (c.grp === 0) {
        if (c.sub === 0) {
          if (c.br === 0) continue;
          if (c.br === 1) {
            pc = a;
            continue;
          }
          if (c.br === 2) return { kind: "end", why: "break" };
        } else if (c.sub === 1) {
          if (c.br === 0 || c.br === 2) return { kind: "jump", object: a, call: c.br === 2, next: pc };
          if (c.br === 1 || c.br === 3) {
            const o = this.titleObject(a);
            if (o == null) return { kind: "end", why: `title ${a} can't be played` };
            return { kind: "jump", object: o, call: c.br === 3, next: pc };
          }
          if (c.br === 4) return { kind: "end", why: "resume" };
        } else if (c.sub === 2) {
          if (c.br <= 2)
            return { kind: "play", playlist: a, item: c.br === 1 ? b : null, mark: c.br === 2 ? b : null, next: pc };
          if (c.br === 3) continue;
          if (c.br === 4 || c.br === 5) return { kind: "link", item: c.br === 4 ? a : null, mark: c.br === 5 ? a : null };
        }
        return { kind: "end", why: `unknown branch ${c.sub}/${c.br}` };
      }
      if (c.grp === 1) {
        skip = !compare(c.cmp, a, b);
        continue;
      }
      if (c.grp === 2 && c.sub === 0) {
        this.setOp(c, a, b);
        continue;
      }
      if (c.grp === 2 && c.sub === 1) {
        const ev = this.setSystem(c, object);
        if (ev) return ev;
        continue;
      }
      return { kind: "end", why: `unknown command group ${c.grp}` };
    }
    return { kind: "end", why: object == null ? "button done" : `object ${object} ran off its end` };
  }
  setOp(c, a, b) {
    switch (c.set) {
      case 1:
        this.put(c.dst, b);
        break;
      case 2:
        this.put(c.dst, b);
        if (!c.imm2) this.put(c.src, a);
        break;
      case 3:
        this.put(c.dst, a + b);
        break;
      case 4:
        this.put(c.dst, a > b ? a - b : 0);
        break;
      case 5:
        this.put(c.dst, Math.imul(a, b));
        break;
      case 6:
        this.put(c.dst, b ? Math.floor(a / b) : 4294967295);
        break;
      case 7:
        this.put(c.dst, b ? a % b : 4294967295);
        break;
      case 8:
        this.put(c.dst, b ? 1 + Math.floor(this.rnd() * b) : 0);
        break;
      // rnd: 1..b
      case 9:
        this.put(c.dst, a & b);
        break;
      case 10:
        this.put(c.dst, a | b);
        break;
      case 11:
        this.put(c.dst, a ^ b);
        break;
      case 12:
        this.put(c.dst, a | 1 << (b & 31));
        break;
      case 13:
        this.put(c.dst, a & ~(1 << (b & 31)));
        break;
      case 14:
        this.put(c.dst, b > 31 ? 0 : a << b);
        break;
      case 15:
        this.put(c.dst, b > 31 ? 0 : a >>> b);
        break;
    }
  }
  setSystem(c, object) {
    const { dst, src } = c;
    const reg = (v, imm, mask) => (imm ? v : this.gpr[v & 4095]) & mask;
    switch (c.set) {
      case 1:
      // set_stream
      case 11:
        if (dst & 2147483648) this.psr[1] = reg(dst >>> 16, c.imm1, 4095);
        if (dst & 32768)
          this.psr[2] = (this.psr[2] & 2147479552 | reg(dst, c.imm1, 4095) | (dst & 16384 ? 2147483648 : 0)) >>> 0;
        if (src & 2147483648) this.psr[0] = reg(src >>> 16, c.imm2, 4095);
        if (src & 32768) this.psr[3] = reg(src, c.imm2, 4095);
        return null;
      case 3: {
        const button = dst & 2147483648 ? reg(dst, c.imm1, 65535) : null;
        const page = src & 2147483648 ? reg(src, c.imm2, 255) : null;
        if (object == null) return { kind: "page", page, button };
        if (button != null) this.psr[10] = button;
        if (page != null) this.psr[11] = page;
        return null;
      }
      case 4:
      // enable_button
      case 5: {
        if (object == null && this.status.kind === "menu")
          this.setEnabled(this.status.menu, this.val(dst, c.imm1) & 65535, c.set === 4);
        return null;
      }
      case 7:
        return null;
      case 2:
        this.unsupported("navigation timers");
        return null;
      default:
        return null;
    }
  }
  unsupported(feature) {
    if (this.seen.has(feature)) return;
    this.seen.add(feature);
    this.log(`not supported: ${feature}`);
    this.unsupportedCb(feature);
  }
};
function describeCell(c) {
  if (c.extra) return c.extra;
  const s = c.season != null ? `S${String(c.season).padStart(2, "0")}` : "";
  return `${s}E${String(c.episode ?? 0).padStart(2, "0")} (${c.file})`;
}

// src/walk.ts
var FIRST_PLAY_LIMIT = 50;
function walkPack(pack2) {
  const media = {};
  for (const [name, pl] of Object.entries(pack2.playlists)) {
    const shows = Object.values(pl.menus).some((k) => {
      const m = pack2.menus[k];
      return m && m.model === "always_on" && !!m.pages?.length;
    });
    if (shows) continue;
    const ticks = pl.items.reduce((s, it) => s + (it.out - it.in) * (it.repeat ?? 1), 0);
    media[name] = { cells: { "0": { file: `walk:${name}`, offset: 0, dur: ticks / TICKS + 1 } } };
  }
  return { ...pack2, media };
}
function hdmvOutcome(pack2, s) {
  if (s.kind === "menu") return { kind: "menu", at: `${s.menu.key} p${s.menu.page.id}` };
  if (s.kind === "stopped") return { kind: "stopped", why: s.why };
  const h = s.handoff;
  const pl = pack2.playlists[h.playlist];
  const mark = pl?.marks.findIndex((m) => m.at === h.start) ?? -1;
  return { kind: "plays", playlist: h.playlist, start: h.start, mark: mark >= 0 ? mark : null, seconds: h.start / TICKS };
}
function walkHdmv(source, opts) {
  const pack2 = walkPack(source);
  const langs = opts.languages?.length ? opts.languages : source.disc.menu_languages?.length ? source.disc.menu_languages : ["eng"];
  const maxPages2 = opts.maxPages ?? 200;
  const unsupported = /* @__PURE__ */ new Set();
  const result = { schema: "walk-1", disc: "bd", first_play: [], pages: [], truncated: false, unsupported: [] };
  const seen = /* @__PURE__ */ new Set();
  for (const lang of [...new Set(langs)]) {
    const fresh = () => {
      const vm = new HdmvVm(pack2, { audioLang: lang, subtitleLang: lang, rnd: () => 0, unsupported: (f) => unsupported.add(f) });
      let s = vm.start();
      for (let i = 0; i < FIRST_PLAY_LIMIT && s.kind === "handoff"; i++) s = vm.ended();
      return vm;
    };
    const replay = (path) => {
      const vm = fresh();
      for (const id of path) {
        vm.select(id);
        const s = vm.activate();
        if (s.kind !== "menu") break;
      }
      return vm;
    };
    if (!result.first_play.length) {
      const vm = new HdmvVm(pack2, { audioLang: lang, subtitleLang: lang, rnd: () => 0 });
      let s = vm.start();
      for (let i = 0; i < FIRST_PLAY_LIMIT && s.kind === "handoff"; i++) {
        result.first_play.push(hdmvOutcome(pack2, s));
        s = vm.ended();
      }
      result.first_play.push(hdmvOutcome(pack2, s));
    }
    const queue = [[]];
    let pages = 0;
    while (queue.length) {
      const path = queue.shift();
      const vm = replay(path);
      if (vm.status.kind !== "menu") continue;
      const m = vm.status.menu;
      const at = `${m.key} p${m.page.id}`;
      const shown = vm.shownButtons();
      const sig = `${at}|${shown.map((b) => `${b.button.id}:${b.button.images.normal[0] ?? ""}`).join(",")}`;
      if (seen.has(sig)) continue;
      if (pages >= maxPages2) {
        result.truncated = true;
        break;
      }
      seen.add(sig);
      pages++;
      const page = { at, menu: m.key, page: m.page.id, playlist: m.playlist, language: lang, path, buttons: [] };
      for (const b of shown) {
        const w = replay(path);
        const selected = w.select(b.button.id);
        const s = b.button.auto ? selected : w.activate();
        const outcome = hdmvOutcome(pack2, s);
        if (outcome.kind === "menu" && outcome.at !== at) queue.push([...path, b.button.id]);
        page.buttons.push({
          id: b.button.id,
          x: b.button.x,
          y: b.button.y,
          w: 0,
          h: 0,
          image: b.button.images.normal[0] ?? b.button.images.selected[0] ?? null,
          outcome
        });
      }
      result.pages.push(page);
    }
  }
  result.unsupported = [...unsupported].sort();
  return result;
}
function settleDvd(vm) {
  for (let i = 0; i < 2e3; i++) {
    if (vm.stopped) return;
    if (vm.state.domain === "VTS") return;
    const c = vm.cell;
    if (!c || c.menu && c.menu.buttons.length || c.still === 255) return;
    vm.cellEnded();
  }
}
function dvdOutcome(vm) {
  if (vm.stopped) return { kind: "stopped", why: "stopped" };
  if (vm.state.domain === "VTS") {
    return { kind: "plays_title", title: vm.sprm[4], chapter: vm.sprm[7], vts: vm.state.vtsN, pgc: vm.state.pgcN, cell: vm.state.cellN };
  }
  const c = vm.cell;
  if (c?.menu?.buttons.length) return { kind: "menu", at: `${vm.where()} cell ${vm.state.cellN}` };
  return { kind: "stopped", why: `no menu at ${vm.where()}` };
}
function walkDvd(pack2, opts) {
  const maxPages2 = opts.maxPages ?? 200;
  const lang = pack2.menu_lang || "en";
  const result = { schema: "walk-1", disc: "dvd", first_play: [], pages: [], truncated: false, unsupported: [] };
  const fresh = () => {
    const vm = new DvdVm(pack2, { rnd: () => 0 });
    vm.start();
    settleDvd(vm);
    for (let i = 0; i < FIRST_PLAY_LIMIT && !vm.stopped && vm.state.domain === "VTS"; i++) {
      vm.cellEnded();
      settleDvd(vm);
    }
    return vm;
  };
  const replay = (path) => {
    const vm = fresh();
    for (const n of path) {
      const b = vm.cell?.menu?.buttons.find((x) => x.n === n);
      if (!b) break;
      vm.activate(b);
      settleDvd(vm);
    }
    return vm;
  };
  result.first_play.push(dvdOutcome(fresh()));
  const seen = /* @__PURE__ */ new Set();
  const queue = [[]];
  while (queue.length) {
    const path = queue.shift();
    const vm = replay(path);
    const here = dvdOutcome(vm);
    if (here.kind !== "menu" || seen.has(here.at)) continue;
    if (seen.size >= maxPages2) {
      result.truncated = true;
      break;
    }
    seen.add(here.at);
    const menu = vm.cell.menu;
    const page = { at: here.at, menu: here.at, page: vm.state.cellN, language: lang, path, buttons: [] };
    for (const b of menu.buttons) {
      const w = replay(path);
      const wb = w.cell?.menu?.buttons.find((x) => x.n === b.n);
      if (wb) {
        w.activate(wb);
        settleDvd(w);
      }
      const outcome = dvdOutcome(w);
      if (outcome.kind === "menu" && outcome.at !== here.at) queue.push([...path, b.n]);
      const [x0, y0, x1, y1] = b.rect;
      page.buttons.push({ id: b.n, x: x0, y: y0, w: x1 - x0, h: y1 - y0, image: menu.background?.image ?? null, outcome });
    }
    result.pages.push(page);
  }
  return result;
}
function walk(pack2, opts = {}) {
  if (pack2.schema === "hdmv-1") return walkHdmv(pack2, opts);
  if (pack2.schema === "dvd-1") return walkDvd(pack2, opts);
  throw new Error(`unknown pack schema ${String(pack2.schema)}`);
}

// src/walk-cli.ts
var args = process.argv.slice(2);
var opt = (name) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : void 0;
};
var file = args.find((a, i) => !a.startsWith("--") && !(i > 0 && args[i - 1].startsWith("--")));
if (!file || args.includes("--version")) {
  process.stdout.write(args.includes("--version") ? `${"0.4.0"}
` : "usage: walk.mjs <menu.json> [--lang eng,jpn] [--max-pages N]\n");
  process.exit(file || args.includes("--version") ? 0 : 2);
}
var pack = JSON.parse(readFileSync(file, "utf8"));
var languages = opt("--lang")?.split(",").filter(Boolean);
var maxPages = opt("--max-pages") ? Number(opt("--max-pages")) : void 0;
process.stdout.write(JSON.stringify({ version: "0.4.0", ...walk(pack, { languages, maxPages }) }) + "\n");

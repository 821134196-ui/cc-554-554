const { createApp } = Vue;

createApp({
  data() {
    return {
      tab: "tourist",
      wind: null,
      slots: [],
      // 游客端
      orderNo: "2026100301",
      order: null,
      form: {},          // ticketId -> { rebookSlot, waitSlot }
      timelines: {},     // ticketId -> actions
      // 调度端
      dash: null,
      autoRefresh: false,
      _timer: null,
      // 运营端
      events: [],
      eventForm: { reason: "weather", selected: [], detail: "", created_by: "运营-周调度" },
      resumeForms: {},
      // 模拟接口
      mockWind: 19,
      payCfg: { callback_delay: 2, auto_callback: true, fail_next: false },
      gateNo: "",
      gateResult: null,
      // 杂项
      toasts: [],
      _toastSeq: 0,
    };
  },
  computed: {
    slotById() {
      const m = {};
      this.slots.forEach(s => { m[s.id] = s; });
      return m;
    },
  },
  methods: {
    // ---------- 基础 ----------
    slotCode(id) {
      const s = this.slotById[id];
      return s ? `${s.code}(${s.start_time})` : `#${id}`;
    },
    fmt(iso) {
      if (!iso) return "";
      const d = new Date(iso);
      const p = n => String(n).padStart(2, "0");
      return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    },
    actionCn(a) {
      return ({
        refund_request: "申请退款", rebook: "立即改签", waitlist_join: "加入候补",
        waitlist_cancel: "撤回候补", waitlist_fill: "候补递补出票",
        issued: "新票出票", check_in: "闸机核销",
      })[a] || a;
    },
    toast(text, type = "info", ms = 3200) {
      const id = ++this._toastSeq;
      this.toasts.push({ id, text, type });
      setTimeout(() => {
        this.toasts = this.toasts.filter(t => t.id !== id);
      }, ms);
    },
    async api(method, url, body) {
      const opt = { method, headers: { "Content-Type": "application/json" } };
      if (body !== undefined) opt.body = JSON.stringify(body);
      const r = await fetch(url, opt);
      let data = null;
      try { data = await r.json(); } catch (e) { data = null; }
      if (!r.ok) {
        const msg = (data && data.error) || `请求失败 ${r.status}`;
        const err = new Error(msg);
        err.code = data && data.code;
        err.status = r.status;
        throw err;
      }
      return data;
    },
    async loadWind() {
      this.wind = await this.api("GET", "/api/meta/wind");
    },
    async loadSlots() {
      const d = await this.api("GET", "/api/slots");
      this.slots = d.slots;
    },
    async switchTab(t) {
      this.tab = t;
      if (t === "dispatch") await this.loadDash();
      if (t === "ops") { await this.loadSlots(); await this.loadEvents(); }
      if (t === "tourist" && this.order) await this.searchOrder();
    },

    // ---------- 游客端 ----------
    async searchOrder() {
      this.timelines = {};
      try {
        this.order = await this.api("GET", `/api/orders/${encodeURIComponent(this.orderNo.trim())}`);
        this.order.tickets.forEach(t => {
          this.form[t.id] = this.form[t.id] || { rebookSlot: "", waitSlot: "" };
          this.loadTimeline(t.id);
        });
      } catch (e) {
        this.order = null;
        this.toast(e.message, "err");
      }
    },
    async loadTimeline(tid) {
      const d = await this.api("GET", `/api/tickets/${tid}/actions`);
      this.timelines[tid] = d.actions;
    },
    async _afterTicket(t) {
      await this.searchOrder();
      await this.loadTimeline(t.id);
    },
    async doRefund(t) {
      try {
        await this.api("POST", `/api/tickets/${t.id}/refund`, {});
        this.toast(`${t.passenger_name} 的退款已受理，等待支付回执`, "ok");
        await this._afterTicket(t);
      } catch (e) { this.toast(e.message, "err"); }
    },
    async doRebook(t) {
      const sid = Number(this.form[t.id].rebookSlot);
      if (!sid) return;
      try {
        const r = await this.api("POST", `/api/tickets/${t.id}/rebook`, { target_slot_id: sid });
        this.toast(`改签成功，新票号 ${r.new_ticket_no}`, "ok");
        await this._afterTicket(t);
      } catch (e) { this.toast(e.message, "err"); }
    },
    async doWaitlist(t) {
      const sid = Number(this.form[t.id].waitSlot);
      if (!sid) return;
      try {
        const r = await this.api("POST", `/api/tickets/${t.id}/waitlist`, { target_slot_id: sid });
        this.toast(`已加入 ${this.slotCode(sid)} 候补队列，第 ${r.position} 位`, "ok");
        await this._afterTicket(t);
      } catch (e) { this.toast(e.message, "err"); }
    },
    async doCancelWaitlist(t) {
      try {
        await this.api("POST", `/api/tickets/${t.id}/waitlist/cancel`, {});
        this.toast("已撤回候补，可重新选择退款或换时段", "info");
        await this._afterTicket(t);
      } catch (e) { this.toast(e.message, "err"); }
    },
    async retryPay(no) {
      try {
        await this.api("POST", `/api/payments/${no}/retry`, {});
        this.toast("已重新发起退款", "info");
        await this.searchOrder();
      } catch (e) { this.toast(e.message, "err"); }
    },

    // ---------- 调度端 ----------
    async loadDash() {
      this.dash = await this.api("GET", "/api/dashboard");
    },
    async setCapacity(s, ev) {
      const cap = Number(ev.target.value);
      if (!(cap >= 0)) return;
      try {
        await this.api("POST", `/api/dispatch/slots/${s.id}/capacity`, { capacity: cap });
        this.toast(`${s.code} 运力调整为 ${cap}`, "ok");
        await this.loadDash();
      } catch (e) {
        this.toast(e.message, "err");
        ev.target.value = s.capacity;
      }
    },
    async fillOne(sid) {
      try {
        const r = await this.api("POST", "/api/dispatch/fill", { slot_id: sid });
        if (r.filled) this.toast(`已递补：新票 ${r.filled.new_ticket_no}`, "ok");
        else this.toast("该班次暂无实际空余运力，未递补", "info");
        await this.loadDash();
      } catch (e) { this.toast(e.message, "err"); }
    },
    async autoFill() {
      try {
        const r = await this.api("POST", "/api/dispatch/auto-fill", {});
        this.toast(r.count ? `FIFO 递补完成，共出票 ${r.count} 张` : "没有可在空余运力内递补的候补",
                   r.count ? "ok" : "info");
        await this.loadDash();
      } catch (e) { this.toast(e.message, "err"); }
    },

    // ---------- 运营端 ----------
    async loadEvents() {
      const d = await this.api("GET", "/api/events");
      this.events = d.events;
      this.events.forEach(ev => {
        this.resumeForms[ev.id] = this.resumeForms[ev.id] ||
          { confirmer: "", isOwner: false, remark: "" };
      });
    },
    toggleSlot(id) {
      const arr = this.eventForm.selected;
      const i = arr.indexOf(id);
      if (i >= 0) arr.splice(i, 1); else arr.push(id);
    },
    async createEvent() {
      if (!this.eventForm.selected.length) {
        this.toast("请至少选择一个停运班次", "err"); return;
      }
      try {
        const r = await this.api("POST", "/api/events", {
          reason: this.eventForm.reason,
          affected_slot_ids: this.eventForm.selected,
          detail: this.eventForm.detail,
          created_by: this.eventForm.created_by,
        });
        this.toast(`停运事件 #${r.event_id} 已创建，关联 ${r.linked_tickets} 张未使用票`, "ok");
        this.eventForm.selected = [];
        this.eventForm.detail = "";
        await Promise.all([this.loadEvents(), this.loadSlots(), this.loadWind()]);
      } catch (e) { this.toast(e.message, "err"); }
    },
    async resumeEvent(ev) {
      const f = this.resumeForms[ev.id];
      if (!f.confirmer.trim()) { this.toast("请填写设备负责人姓名", "err"); return; }
      if (!f.isOwner) { this.toast("必须由设备负责人本人勾选确认", "err"); return; }
      try {
        await this.api("POST", `/api/events/${ev.id}/resume`, {
          confirmer: f.confirmer, role: "equipment_owner", remark: f.remark,
        });
        this.toast(`事件 #${ev.id} 已恢复运营`, "ok");
        await Promise.all([this.loadEvents(), this.loadWind()]);
      } catch (e) { this.toast(e.message, "err"); }
    },

    // ---------- 模拟接口 ----------
    async setWind() {
      try {
        await this.api("POST", "/api/mock/wind", { speed: Number(this.mockWind) });
        await this.loadWind();
        this.toast(`风速已设为 ${this.mockWind} m/s`, "ok");
      } catch (e) { this.toast(e.message, "err"); }
    },
    async loadPayCfg() {
      this.payCfg = await this.api("GET", "/api/mock/payment/config");
    },
    async savePayCfg() {
      try {
        await this.api("POST", "/api/mock/payment/config", {
          callback_delay: Number(this.payCfg.callback_delay),
          auto_callback: !!this.payCfg.auto_callback,
          fail_next: !!this.payCfg.fail_next,
        });
        this.toast("支付网关配置已更新", "ok");
      } catch (e) { this.toast(e.message, "err"); }
    },
    async deliver(no, status) {
      try {
        await this.api("POST", "/api/mock/payment/deliver", { payment_no: no, status });
        this.toast(`${status === "succeeded" ? "成功" : "失败"}回执已送达：${no}`, "info");
        if (this.dash) await this.loadDash();
        if (this.order) await this.searchOrder();
      } catch (e) { this.toast(e.message, "err"); }
    },
    async gateCheck() {
      const no = this.gateNo.trim();
      if (!no) return;
      try {
        const r = await this.api("POST", "/api/gate/checkin", { ticket_no: no });
        this.gateResult = {
          ok: true,
          text: r.duplicate
            ? `重复过闸（票号 ${r.ticket_no} 已有核销记录），已登记。`
            : `✅ 放行：${r.ticket_no} 已核销。`,
        };
      } catch (e) {
        this.gateResult = { ok: false, text: `⛔ ${e.message}` };
      }
    },

    // ---------- 自动刷新 ----------
    startTimer() {
      this._timer = setInterval(async () => {
        if (this.autoRefresh && this.tab === "dispatch") {
          try { await this.loadDash(); } catch (e) {}
        }
        try { await this.loadWind(); } catch (e) {}
      }, 4000);
    },
  },
  async mounted() {
    await Promise.all([this.loadWind(), this.loadSlots(), this.loadPayCfg()]);
    await this.searchOrder();
    this.startTimer();
  },
}).mount("#app");

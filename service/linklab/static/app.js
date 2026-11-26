/* 园区上行调速台 frontend logic (vanilla JS). */
(function () {
  "use strict";

  var initial = JSON.parse(document.getElementById("initialConfig").textContent);
  var sparkData = { alarm: [], video: [] };
  var SPARK_MAX = 40;

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function mbps(kbps) { return (Number(kbps || 0) / 1000).toFixed(2); }

  async function postJSON(url, body) {
    var res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
    var data = await res.json().catch(function () { return {}; });
    data._status = res.status;
    return data;
  }
  async function getJSON(url) {
    var res = await fetch(url);
    return res.json();
  }

  // ---------------------------------------------------------------------
  // Form population + live invariant
  // ---------------------------------------------------------------------
  function fillForm(cfg) {
    $("total_mbps").value = mbps(cfg.total_kbps);
    $("alarm_rate_mbps").value = mbps(cfg.alarm.rate_kbps);
    $("alarm_ceil_mbps").value = mbps(cfg.alarm.ceil_kbps);
    $("video_rate_mbps").value = mbps(cfg.video.rate_kbps);
    $("video_ceil_mbps").value = mbps(cfg.video.ceil_kbps);
    updateFormInvariant();
  }

  function readForm() {
    return {
      total_mbps: parseFloat($("total_mbps").value),
      alarm_rate_mbps: parseFloat($("alarm_rate_mbps").value),
      alarm_ceil_mbps: parseFloat($("alarm_ceil_mbps").value),
      video_rate_mbps: parseFloat($("video_rate_mbps").value),
      video_ceil_mbps: parseFloat($("video_ceil_mbps").value)
    };
  }

  function kbps(v) { return Math.round((parseFloat(v) || 0) * 1000); }

  function updateFormInvariant() {
    var f = readForm();
    var T = kbps(f.total_mbps);
    var ar = kbps(f.alarm_rate_mbps), ac = kbps(f.alarm_ceil_mbps);
    var vr = kbps(f.video_rate_mbps), vc = kbps(f.video_ceil_mbps);
    var sum = ar + vr;
    var errs = [];
    if (!(T > 0)) errs.push("总速率需为正数");
    if (sum > T) errs.push("保底之和 " + mbps(sum) + " > 总速率 " + mbps(T));
    if (ac > T) errs.push("告警上限 " + mbps(ac) + " > 总速率 " + mbps(T));
    if (vc > T) errs.push("录像上限 " + mbps(vc) + " > 总速率 " + mbps(T));
    if (ar > ac) errs.push("告警保底 > 告警上限");
    if (vr > vc) errs.push("录像保底 > 录像上限");
    var box = $("formInvariant");
    if (errs.length) {
      box.className = "invariant bad";
      box.innerHTML = "⚠ " + errs.map(esc).join("；");
    } else {
      box.className = "invariant ok";
      box.innerHTML = "✓ 目标配置合法：保底之和 <b>" + mbps(sum) +
        "</b> ≤ 总速率 <b>" + mbps(T) + "</b>；两类上限均 ≤ " + mbps(T) +
        "。提交时后端会保证<b>调整与回退的每一步</b>都满足该约束。";
    }
    updateGuaranteeMarkers(sum, T);
  }

  function updateGuaranteeMarkers(sum, T) {
    ["alarm", "video"].forEach(function (k) {
      var rate = kbps($(k + (k === "alarm" ? "_rate_mbps" : "_rate_mbps")).value);
      var marker = $("gbar-" + k);
      if (marker && T > 0) marker.style.left = Math.min(100, rate / T * 100) + "%";
    });
  }

  ["total_mbps", "alarm_rate_mbps", "alarm_ceil_mbps",
   "video_rate_mbps", "video_ceil_mbps"].forEach(function (id) {
    $(id).addEventListener("input", updateFormInvariant);
  });

  // ---------------------------------------------------------------------
  // Banner + plan/result rendering
  // ---------------------------------------------------------------------
  function showBanner(ok, html) {
    var b = $("actionBanner");
    b.className = "banner " + (ok ? "ok" : "err");
    b.innerHTML = html;
    b.classList.remove("hidden");
  }

  function stepTable(steps, opts) {
    opts = opts || {};
    if (!steps || !steps.length) return "<p class='muted small'>无变更步骤（配置未变化）。</p>";
    var rows = steps.map(function (s) {
      var status;
      if (s.rc === 0 || s.ok) status = "<span class='tag tag-ok'>成功</span>";
      else if (s.rc != null) status = "<span class='tag tag-fail'>失败 rc=" + esc(s.rc) + "</span>";
      else status = "<span class='tag tag-run'>待执行</span>";
      var inv = s.planned_invariant_ok === false
        ? "<span class='tag tag-fail'>越界!</span>"
        : "<span class='tag tag-ok'>不变量✓</span>";
      var cmd = s.cmd || (s.argv ? esc(s.argv.join(" ")) : "");
      var err = s.stderr ? "<pre class='cmd errout'>" + esc(s.stderr) + "</pre>" : "";
      return "<tr>" +
        "<td>" + esc(s.seq) + "</td>" +
        "<td>" + status + (s.fault_injected ? " <span class='tag tag-fail'>注入</span>" : "") + "</td>" +
        "<td>" + esc(s.note || "") +
          "<pre class='cmd'>" + esc(cmd) + "</pre>" + err + "</td>" +
        "<td class='mono'>总 " + mbps(s.effective_total_kbps) + "M<br>" + inv + "</td>" +
        "</tr>";
    }).join("");
    return "<table class='step-table'><thead><tr>" +
      "<th>#</th><th>状态</th><th>操作 / 命令 / 返回</th><th>当时总速率与校验</th>" +
      "</tr></thead><tbody>" + rows + "</tbody></table>";
  }

  function renderPlan(planResp) {
    var view = $("planView");
    if (!planResp.valid) {
      view.innerHTML = "<div class='banner err'>校验未通过：<br>" +
        (planResp.errors || []).map(esc).join("<br>") + "</div>";
      return;
    }
    view.innerHTML =
      "<details open><summary>预览：" + planResp.plan.step_count +
      " 个安全步骤（仅 tc class change，不重建队列）</summary>" +
      stepTable(planResp.plan.steps.map(function (s) {
        return {
          seq: s.seq, note: s.operation.note, cmd: s.cmd,
          effective_total_kbps: s.effective_total_kbps,
          planned_invariant_ok: s.invariant_ok
        };
      })) + "</details>";
  }

  function renderResult(r) {
    var html = "";
    if (r.ok) {
      showBanner(true, "✅ 调速成功，全部 " + (r.steps || []).length +
        " 步已在发包过程中在线生效，原 HTB 队列（handle 1:）未被删除重建。");
    } else if (r.rejected) {
      showBanner(false, "🚫 未执行任何变更：" + esc(r.reason));
    } else {
      var rb = r.rollback || {};
      showBanner(false,
        "💥 <b>调速中途失败</b>：" + esc(r.reason) +
        (rb.ok
          ? "<br>↩ 已按同样的安全顺序回退，改前配置已恢复（" +
            (((rb.steps || []).length === 0)
              ? "失败步骤未生效，无需回退"
              : "回退 " + rb.steps.length + " 步全部成功") + "）。"
          : "<br>⚠ 回退也失败了：" + esc(rb.reason || "未知原因") +
            "，请检查内核配置！"));
    }

    if (r.steps && r.steps.length) {
      html += "<details open><summary>变更步骤（" + r.steps.length + "）</summary>" +
        stepTable(r.steps) + "</details>";
    }
    if (r.rollback && r.rollback.steps && r.rollback.steps.length) {
      html += "<details open><summary>↩ 回退步骤（每步仍满足不变量）</summary>" +
        stepTable(r.rollback.steps.map(function (s) {
          return Object.assign({ fault_injected: false }, s);
        })) + "</details>";
    }
    $("resultView").innerHTML = html;
    if (r.config_after) fillForm(r.config_after);
    loadKernel();
  }

  // ---------------------------------------------------------------------
  // Config actions
  // ---------------------------------------------------------------------
  $("btnPreview").addEventListener("click", async function () {
    var body = readForm();
    var r = await postJSON("/api/config/validate", body);
    renderPlan(r);
  });

  $("configForm").addEventListener("submit", async function (e) {
    e.preventDefault();
    var btn = e.submitter;
    if (btn) btn.disabled = true;
    var body = readForm();
    if ($("faultEnabled").checked) {
      body.fault_step = parseInt($("faultStep").value || "2", 10);
      body.fault_mode = $("faultMode").value;
    }
    try {
      var r = await postJSON("/api/config/apply", body);
      renderResult(r);
    } catch (err) {
      showBanner(false, "请求失败：" + esc(err));
    } finally {
      if (btn) btn.disabled = false;
    }
  });

  $("btnDemoFail").addEventListener("click", async function () {
    var btn = $("btnDemoFail");
    btn.disabled = true;
    try {
      var r = await postJSON("/api/demo/failure", {});
      renderResult(r);
    } catch (err) {
      showBanner(false, "演示请求失败：" + esc(err));
    } finally {
      btn.disabled = false;
    }
  });

  // ---------------------------------------------------------------------
  // Flows
  // ---------------------------------------------------------------------
  document.querySelectorAll(".flow-ctrl button[data-act]").forEach(function (b) {
    b.addEventListener("click", async function () {
      var key = b.dataset.flow, act = b.dataset.act;
      if (act === "start") {
        var rate = $( "rate-" + key).value;
        await postJSON("/api/flows/" + key + "/start", { rate_mbps: parseFloat(rate) });
      } else {
        await postJSON("/api/flows/" + key + "/stop", {});
      }
      loadFlows();
    });
  });
  $("btnStartBoth").addEventListener("click", async function () {
    ["alarm", "video"].forEach(async function (key) {
      await postJSON("/api/flows/" + key + "/start",
        { rate_mbps: parseFloat($("rate-" + key).value) });
    });
    setTimeout(loadFlows, 500);
  });
  $("btnStopAll").addEventListener("click", async function () {
    await postJSON("/api/flows/stop-all", {});
    loadFlows();
  });

  // ---------------------------------------------------------------------
  // DSCP re-marking (alarm flow; one switch per sending round)
  // ---------------------------------------------------------------------
  $("btnRemarkAlarm").addEventListener("click", async function () {
    var btn = $("btnRemarkAlarm");
    var raw = $("dscp-alarm-input").value;
    var dscp = parseInt(raw, 10);
    if (!(dscp >= 1 && dscp <= 63)) {
      showDscpBanner(false, "新 DSCP 必须是 1~63 的整数，当前输入: " + esc(raw));
      return;
    }
    btn.disabled = true;
    try {
      var r = await postJSON("/api/flows/alarm/remark", { new_dscp: dscp });
      if (r.ok) {
        showDscpBanner(true,
          "✅ 换标已生效：新标记 <b>DSCP " + esc(r.new_dscp) +
          " / " + esc(r.ecn === 1 ? "ECT(0)" : r.ecn) +
          "</b> 从序号 <b>" + esc(r.boundary_seq) + "</b> 起用；" +
          "序号连续、ECN 两位保留。等待收端确认新段实收标记…");
      } else {
        showDscpBanner(false, "⚠ 换标未生效，发端继续使用旧标记 DSCP 0 / ECT(0)：" +
          esc(r.reason || "未知原因") + "（录像/告警试发不受影响，继续发包）");
      }
    } catch (err) {
      showDscpBanner(false, "换标请求失败：" + esc(err));
    } finally {
      btn.disabled = false;
      loadFlows();
    }
  });

  function showDscpBanner(ok, html) {
    var b = $("dscp-alarm-banner");
    b.className = "banner " + (ok ? "ok" : "err");
    b.innerHTML = html;
    b.classList.remove("hidden");
  }

  function segRow(title, seg, boundaryNote) {
    if (!seg) {
      return "<tr><td>" + title + "</td><td colspan='5' class='muted'>" +
        esc(boundaryNote || "未开始") + "</td></tr>";
    }
    var statusTag = seg.confirmed
      ? "<span class='tag tag-ok'>已确认</span>"
      : "<span class='tag tag-run'>待确认</span>";
    var obs = Object.keys(seg.observed || {}).length
      ? esc(JSON.stringify(seg.observed))
      : "<span class='muted'>—</span>";
    var badCls = seg.mismatch ? " class='cell-bad'" : "";
    return "<tr>" +
      "<td>" + title + "<div class='small muted'>期望 TOS 0x" +
        Number(seg.expected_tos).toString(16).padStart(2, "0") + "</div></td>" +
      "<td>" + statusTag + "</td>" +
      "<td>" + esc(seg.expected_dscp) + "</td>" +
      "<td>" + esc(seg.expected_ecn_name) + "</td>" +
      "<td" + badCls + ">" + esc(seg.packets) + " 包 / 错标 <b>" +
        esc(seg.mismatch) + "</b>" +
        (seg.dscp_mismatch ? "（DSCP错 " + esc(seg.dscp_mismatch) + "）" : "") +
        (seg.ecn_mismatch ? "（ECN错 " + esc(seg.ecn_mismatch) + "）" : "") +
        "<div class='small muted'>实收TOS分布: " + obs + "</div></td>" +
      "</tr>";
  }

  function renderDscp(flow) {
    var view = $("dscp-alarm-view");
    var rx = flow && flow.rx ? flow.rx : {};
    var marking = rx.marking;
    var ctl = flow && flow.marking_ctl ? flow.marking_ctl : {};
    if (!marking) {
      view.innerHTML = "<p class='muted small'>收端尚未上报标记核验数据（接收器未运行或还未收到包）。</p>";
      return;
    }
    var segs = marking.segments || {};
    var boundary = marking.boundary_seq;
    var boundaryText = boundary == null
      ? "尚未换标（全部报文按旧段 DSCP 0 / ECT(0) 核验）"
      : "分界序号 <b>" + boundary + "</b>：seq &lt; " + boundary + " 为旧段，seq ≥ " +
        boundary + " 为新段（晚到的旧包仍按序号归旧段）";
    var head = "<p class='small' style='margin:.2rem 0 .5rem'>" + boundaryText + "</p>";
    var table = "<table class='dscp-table'><thead><tr>" +
      "<th>分段</th><th>状态</th><th>期望DSCP</th><th>期望ECN</th><th>实收核验（有界计数）</th>" +
      "</tr></thead><tbody>" +
      segRow("旧段 DSCP 0", segs.old) +
      segRow("新段 DSCP " + (marking.new_dscp == null ? "?" : marking.new_dscp),
             segs.new, "待确认（新段尚未收到任何包）") +
      "</tbody></table>";
    var summary;
    if (marking.verified) {
      summary = "<p class='dscp-ok'>✓ 核验通过：新段已收到包且两段实收 DSCP/ECN 全部符合期望，错标 0。</p>";
    } else if (marking.plan_installed && segs.new && !segs.new.confirmed) {
      summary = "<p class='dscp-pending'>⏳ 新段待确认：分界已发布，但收端还没收到序号 ≥ " +
        boundary + " 的包。</p>";
    } else if (marking.total_mismatch) {
      summary = "<p class='dscp-bad'>✗ 发现错标包 " + marking.total_mismatch +
        " 个（unknown_tos=" + marking.unknown_tos +
        "，无法解析=" + marking.unparsable_packets + "）。</p>";
    } else {
      summary = "<p class='muted small'>当前错标 0，总包数 " + marking.total_packets +
        (ctl.error ? "；发端曾报告换标问题: " + esc(ctl.error) : "") + "</p>";
    }
    view.innerHTML = head + table + summary;
  }

  function drawSpark(key, value) {
    var data = sparkData[key];
    data.push(value);
    if (data.length > SPARK_MAX) data.shift();
    var svg = $("spark-" + key);
    var w = svg.clientWidth || 320, h = 42;
    svg.setAttribute("viewBox", "0 0 " + w + " " + h);
    var total = kbps($("total_mbps").value) / 1000 || initial.total_kbps / 1000;
    var max = Math.max(total, 1, Math.max.apply(null, data));
    var pts = data.map(function (v, i) {
      var x = data.length === 1 ? 0 : i / (SPARK_MAX - 1) * w;
      var y = h - Math.min(1, v / max) * (h - 6) - 2;
      return x.toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
    var color = key === "alarm" ? "#dc2626" : "#2563eb";
    svg.innerHTML =
      "<polyline points='" + pts + "' fill='none' stroke='" + color +
      "' stroke-width='1.6'/>";
  }

  async function loadFlows() {
    var s;
    try { s = await getJSON("/api/flows"); } catch (e) { return; }
    $("receiverState").textContent = s.disabled
      ? "流量管理未启用"
      : ("接收器: " + (s.receiver_alive ? "运行中 pid=" + s.receiver_pid : "未运行"));
    ["alarm", "video"].forEach(function (key) {
      var f = (s.flows || {})[key] || {};
      var rx = f.rx || {};
      var tx = f.sender || {};
      var rate = Number(rx.rate_mbps || 0);
      $( "rx-" + key).textContent = rate.toFixed(3);
      $("pk-" + key).textContent = rx.packets_total || 0;
      $("loss-" + key).textContent = (rx.seq_loss || 0) +
        (rx.duplicates ? " / 重复" + rx.duplicates : "");
      $("pid-" + key).textContent = f.running ? f.pid : "—";
      $("tx-" + key).textContent = tx.send_rate_mbps != null
        ? Number(tx.send_rate_mbps).toFixed(2) + "M"
        : "—";
      document.querySelector('.flow-card[data-flow="' + key + '"]')
        .classList.toggle("running", !!f.running);
      var total = kbps($("total_mbps").value) / 1000 || initial.total_kbps / 1000;
      $("bar-" + key).style.width = Math.min(100, rate / total * 100) + "%";
      drawSpark(key, rate);
    });
    renderDscp((s.flows || {}).alarm || {});
  }

  // ---------------------------------------------------------------------
  // Kernel view
  // ---------------------------------------------------------------------
  function renderKernel(k) {
    $("kernelMeta").textContent = "更新于 " + k.generated_at;
    $("kernelView").innerHTML = k.sections.map(function (sec) {
      return "<section class='" + (sec.rc ? "rc-nonzero" : "") + "'>" +
        "<h3>" + esc(sec.title) + " <span class='muted small'>rc=" + esc(sec.rc) +
        " · " + esc(sec.cmd || "") + "</span></h3>" +
        "<pre>" + esc(sec.stdout || sec.stderr || "(无输出)") + "</pre></section>";
    }).join("");
  }
  async function loadKernel() {
    try { renderKernel(await getJSON("/api/kernel")); } catch (e) { /* keep old */ }
  }
  $("btnKernelRefresh").addEventListener("click", loadKernel);

  // ---------------------------------------------------------------------
  // Boot + timers
  // ---------------------------------------------------------------------
  fillForm(initial);
  loadFlows();
  loadKernel();
  setInterval(loadFlows, 1000);
  setInterval(function () { if ($("kernelAuto").checked) loadKernel(); }, 5000);
})();

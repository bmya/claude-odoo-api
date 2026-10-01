// Progressive enhancement for the console forms. Everything here works without
// it: the picker degrades to plain checkboxes, the methods section stays visible.
(function () {
  "use strict";

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text) n.textContent = text;
    return n;
  }

  // Checkboxes -> a menu plus removable tags. The checkboxes stay the source of
  // truth (and what gets posted); this only draws them differently.
  function enhancePicker(picker) {
    var boxes = Array.prototype.slice.call(picker.querySelectorAll('input[name="methods"]'));
    var tags = el("div", "tags");
    var select = el("select", "add-method");
    select.setAttribute("aria-label", "Agregar método");

    function draw() {
      tags.textContent = "";
      select.textContent = "";
      select.appendChild(el("option", null, "Agregar método…")).value = "";
      boxes.forEach(function (box) {
        var stale = box.closest(".pick").classList.contains("stale");
        if (box.checked) {
          var tag = el("span", "tag" + (stale ? " stale" : ""));
          if (stale) tag.title = "El servidor no lo permite: no hace nada hasta que se agregue a ODOO_MCP_ALLOWED_METHODS";
          tag.appendChild(el("code", null, box.value));
          var x = el("button", "x", "×");
          x.type = "button";
          x.setAttribute("aria-label", "Quitar " + box.value);
          x.addEventListener("click", function () { box.checked = false; draw(); });
          tag.appendChild(x);
          tags.appendChild(tag);
        } else if (!stale) {
          var opt = el("option", null, box.value);
          opt.value = box.value;
          select.appendChild(opt);
        }
      });
      if (!tags.children.length) tags.appendChild(el("span", "muted", "Ningún método elegido todavía."));
      select.disabled = select.options.length === 1;
    }

    select.addEventListener("change", function () {
      boxes.forEach(function (box) { if (box.value === select.value) box.checked = true; });
      draw();
    });

    Array.prototype.forEach.call(picker.querySelectorAll(".pick"), function (p) { p.hidden = true; });
    picker.appendChild(tags);
    picker.appendChild(select);
    draw();
  }

  function wireMethods(fieldset) {
    var picker = fieldset.querySelector("[data-picker]");
    enhancePicker(picker);
    function sync() {
      var on = fieldset.querySelector('input[name="methods_mode"]:checked');
      picker.hidden = !on || on.value !== "list";
    }
    fieldset.addEventListener("change", sync);
    sync();

    // Minting: the section only means something for a readwrite key.
    var form = fieldset.form || fieldset.closest("form");
    var modes = form ? form.querySelectorAll('input[name="mode"]') : [];
    if (modes.length) {
      var syncMode = function () {
        var m = form.querySelector('input[name="mode"]:checked');
        fieldset.hidden = !m || m.value !== "readwrite";
      };
      Array.prototype.forEach.call(modes, function (r) { r.addEventListener("change", syncMode); });
      syncMode();
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(document.querySelectorAll("[data-methods]"), wireMethods);
  });
})();

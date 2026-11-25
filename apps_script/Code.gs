/**
 * Google Apps Script — Integração Planilha Google Sheets -> Hermes (iFood / 99Food)
 *
 * Como configurar na sua planilha:
 * 1. Crie as abas: "Lojas", "Horarios", "Cardapio", "Auditoria_Sync".
 * 2. Extensões -> Apps Script -> cole este código.
 * 3. Recarregue a planilha e use o menu "Hermes iFood/99":
 *    - "Configurar URL e Segredo" (salva URL pública do Hermes + segredo HMAC).
 *    - "Instalar gatilho automático ao editar" (cria trigger instalável onSheetEditTrigger).
 *    - "Publicar alterações agora" (força envio imediato do snapshot assinado).
 */

var TARGET_TABS = ['Lojas', 'Horarios', 'Cardapio'];

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Hermes iFood/99')
    .addItem('Publicar alterações agora', 'publishSnapshotNow')
    .addItem('Instalar gatilho automático ao editar', 'installEditTrigger')
    .addItem('Configurar acesso Hermes', 'promptConfiguration')
    .addToUi();
}

function promptConfiguration() {
  var ui = SpreadsheetApp.getUi();
  var props = PropertiesService.getScriptProperties();

  var urlResp = ui.prompt(
    'URL do Webhook Hermes',
    'Ex.: https://seu-hermes.com/api/plugins/ifood-loja/sheets/webhook',
    ui.ButtonSet.OK_CANCEL
  );
  if (urlResp.getSelectedButton() !== ui.Button.OK) return;

  var secResp = ui.prompt(
    'Segredo HMAC (IFOOD_LOJA_SHEETS_WEBHOOK_SECRET)',
    'Informe o mesmo segredo configurado no servidor Hermes:',
    ui.ButtonSet.OK_CANCEL
  );
  if (secResp.getSelectedButton() !== ui.Button.OK) return;

  var tokenResp = ui.prompt(
    'Token de acesso Hermes (opcional)',
    'Cole o token aceito pelo middleware Hermes. Deixe vazio apenas se o endpoint webhook for público.',
    ui.ButtonSet.OK_CANCEL
  );
  if (tokenResp.getSelectedButton() !== ui.Button.OK) return;

  props.setProperties({
    HERMES_WEBHOOK_URL: urlResp.getResponseText().trim(),
    HERMES_WEBHOOK_SECRET: secResp.getResponseText().trim(),
    HERMES_AUTH_TOKEN: tokenResp.getResponseText().trim()
  });
  ui.alert('Configuração salva nas Propriedades do Script.');
}

function installEditTrigger() {
  var ss = SpreadsheetApp.getActive();
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'onSheetEditTrigger') {
      ScriptApp.deleteTrigger(triggers[i]);
    }
  }
  ScriptApp.newTrigger('onSheetEditTrigger')
    .forSpreadsheet(ss)
    .onEdit()
    .create();
  SpreadsheetApp.getUi().alert('Gatilho automático instalado com sucesso!');
}

function onSheetEditTrigger(e) {
  if (!e || !e.range) return;
  var sheetName = e.range.getSheet().getName();
  if (TARGET_TABS.indexOf(sheetName) === -1) return;

  var lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return;
  try {
    Utilities.sleep(1500);
    sendSnapshotToHermes('APPS_SCRIPT_ON_EDIT', {
      edited_tab: sheetName,
      edited_a1: e.range.getA1Notation()
    });
  } finally {
    lock.releaseLock();
  }
}

function publishSnapshotNow() {
  var res = sendSnapshotToHermes('APPS_SCRIPT_MANUAL', {});
  SpreadsheetApp.getUi().alert('Resposta do Hermes:\n' + JSON.stringify(res, null, 2));
}

function readSheetValues_(ss, name) {
  var sh = ss.getSheetByName(name);
  if (!sh) return [];
  // getValues() serializa horários em Date ISO; getDisplayValues() preserva HH:MM.
  return sh.getDataRange().getDisplayValues();
}

function computeHmacHex_(message, secret) {
  var sigBytes = Utilities.computeHmacSha256Signature(
    message,
    secret,
    Utilities.Charset.UTF_8
  );
  return sigBytes
    .map(function (b) {
      var v = (b < 0 ? b + 256 : b).toString(16);
      return v.length === 1 ? '0' + v : v;
    })
    .join('');
}

function sendSnapshotToHermes(triggerSource, meta) {
  var props = PropertiesService.getScriptProperties();
  var url = props.getProperty('HERMES_WEBHOOK_URL');
  var secret = props.getProperty('HERMES_WEBHOOK_SECRET');
  var authToken = props.getProperty('HERMES_AUTH_TOKEN');
  if (!url || !secret) {
    throw new Error('Configure HERMES_WEBHOOK_URL e HERMES_WEBHOOK_SECRET primeiro.');
  }

  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var payloadObj = {
    spreadsheet_id: ss.getId(),
    trigger_source: triggerSource,
    metadata: meta || {},
    stores_rows: readSheetValues_(ss, 'Lojas'),
    hours_rows: readSheetValues_(ss, 'Horarios'),
    catalog_rows: readSheetValues_(ss, 'Cardapio')
  };

  var rawBody = JSON.stringify(payloadObj);
  var timestamp = String(Math.floor(Date.now() / 1000));
  var signature = 'sha256=' + computeHmacHex_(timestamp + '.' + rawBody, secret);

  var response = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json; charset=utf-8',
    payload: rawBody,
    headers: Object.assign({
      'X-Hermes-Sheets-Timestamp': timestamp,
      'X-Hermes-Sheets-Signature': signature
    }, authToken ? {
      'X-Hermes-Session-Token': authToken,
      'Authorization': 'Bearer ' + authToken
    } : {}),
    muteHttpExceptions: true
  });

  var code = response.getResponseCode();
  var bodyText = response.getContentText();
  var parsed = {};
  try {
    parsed = JSON.parse(bodyText);
  } catch (err) {
    parsed = { status_code: code, raw: bodyText };
  }

  // O Hermes grava o retorno em Auditoria_Sync no schema canônico A:H.
  // Não adicionar uma segunda linha local (com outro formato) aqui.
  return parsed;
}

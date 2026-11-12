package local.meetnotes;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.net.http.SslCertificate;
import android.net.http.SslError;
import android.os.Bundle;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebStorage;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.ConnectException;
import java.net.HttpURLConnection;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.net.UnknownHostException;
import java.security.MessageDigest;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;

import javax.net.ssl.HostnameVerifier;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSession;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

/**
 * Meetnotes for Android.
 *
 * The app is a window onto the Meetnotes app that your computer serves on
 * your Wi-Fi (https://<computer>:47801). Your computer's certificate is
 * self-signed, so instead of trusting certificate authorities the app pins
 * the SHA-256 fingerprint it received while pairing: only your computer is
 * accepted, and there is no browser warning page.
 *
 * Pairing: type the computer's address and the 6-digit code from
 * Meetnotes -> Asetukset -> Puhelin, or open a meetnotes://pair link.
 */
public class MainActivity extends Activity {
    private static final String PREFS = "meetnotes";
    private static final int REQ_MIC = 1;
    private static final int DEFAULT_PORT = 47801;

    private static final int BG = 0xFF0F1115;
    private static final int SURFACE = 0xFF171A21;
    private static final int BORDER = 0xFF2A2F3A;
    private static final int TEXT = 0xFFE8EAF0;
    private static final int MUTED = 0xFFA1A9B8;
    private static final int ACCENT = 0xFF2DD4BF;
    private static final int ACCENT_INK = 0xFF062A25;
    private static final int ERROR = 0xFFFF6369;

    private WebView web;
    private PermissionRequest pendingPermission;
    private String base;         // https://192.168.1.20:47801
    private String token;
    private String fingerprint;  // lowercase hex SHA-256 of the computer's certificate

    // ------------------------------------------------------------------ lifecycle

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setStatusBarColor(BG);
        getWindow().setNavigationBarColor(BG);
        SharedPreferences p = getSharedPreferences(PREFS, MODE_PRIVATE);
        base = p.getString("base", null);
        token = p.getString("token", null);
        fingerprint = p.getString("fp", null);
        if (handleLink(getIntent())) return;
        if (isPaired()) openApp(); else showPairing(null);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        handleLink(intent);
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack(); else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        if (web != null) web.destroy();
        super.onDestroy();
    }

    private boolean isPaired() {
        return base != null && token != null && fingerprint != null;
    }

    /** meetnotes://pair?u=https%3A%2F%2F192.168.1.20%3A47801&t=TOKEN&fp=SHA256 */
    private boolean handleLink(Intent intent) {
        Uri u = intent == null ? null : intent.getData();
        if (u == null || !"meetnotes".equals(u.getScheme())) return false;
        String server = u.getQueryParameter("u");
        String t = u.getQueryParameter("t");
        String fp = u.getQueryParameter("fp");
        if (server == null || !server.startsWith("https://") || t == null || fp == null || fp.length() != 64) {
            showPairing("Linkki oli vajaa. Syötä osoite ja koodi.");
            return true;
        }
        savePairing(server, t, fp);
        openApp();
        return true;
    }

    private void savePairing(String server, String t, String fp) {
        base = server.replaceAll("/+$", "");
        token = t;
        fingerprint = fp.toLowerCase().replace(":", "");
        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                .putString("base", base).putString("token", token).putString("fp", fingerprint).apply();
    }

    private void unpair() {
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().clear().apply();
        base = token = fingerprint = null;
        if (web != null) {
            web.clearCache(true);
            web.destroy();
            web = null;
        }
        WebStorage.getInstance().deleteAllData();
        CookieManager.getInstance().removeAllCookies(null);
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        showPairing(null);
    }

    // ------------------------------------------------------------------ app (WebView)

    private void openApp() {
        if (web != null) web.destroy();
        web = new WebView(this);
        web.setBackgroundColor(BG);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        web.addJavascriptInterface(new Bridge(), "MeetnotesAndroid");

        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                // The computer's certificate is self-signed: accept it only if it is
                // exactly the one we paired with, on the computer we paired with.
                String fp = fingerprintOf(error.getCertificate());
                if (fp != null && fp.equals(fingerprint) && sameServer(error.getUrl())) {
                    handler.proceed();
                } else {
                    handler.cancel();
                    showOffline("Tietokoneen varmenne ei täsmää. Jos asensit Meetnotesin uudelleen, "
                            + "yhdistä puhelin uudelleen.");
                }
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                if (sameServer(url)) return false;
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                } catch (Exception ignored) {
                }
                return true;
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    showOffline("Tietokoneeseen ei saada yhteyttä. Onko Meetnotes käynnissä, puhelinyhteys "
                            + "päällä ja puhelin samassa Wi-Fi-verkossa?");
                }
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                runOnUiThread(new Runnable() {
                    public void run() {
                        boolean audio = false;
                        for (String r : request.getResources()) {
                            if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(r)) audio = true;
                        }
                        if (!audio || !sameServer(request.getOrigin().toString())) {
                            request.deny();
                        } else if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                                == PackageManager.PERMISSION_GRANTED) {
                            request.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
                        } else {
                            pendingPermission = request;
                            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC);
                        }
                    }
                });
            }
        });

        setContentView(web);
        web.loadUrl(base + "/?t=" + Uri.encode(token));
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] permissions, int[] results) {
        if (code != REQ_MIC || pendingPermission == null) return;
        if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
            pendingPermission.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
        } else {
            pendingPermission.deny();
        }
        pendingPermission = null;
    }

    private boolean sameServer(String url) {
        if (url == null || base == null) return false;
        return url.equals(base) || url.startsWith(base + "/");
    }

    /** Called from the page: window.MeetnotesAndroid.* */
    private class Bridge {
        @JavascriptInterface
        public void keepScreenOn(final boolean on) {
            runOnUiThread(new Runnable() {
                public void run() {
                    if (on) getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                    else getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                }
            });
        }

        @JavascriptInterface
        public void unpair() {
            runOnUiThread(new Runnable() {
                public void run() {
                    MainActivity.this.unpair();
                }
            });
        }

        @JavascriptInterface
        public String version() {
            try {
                return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
            } catch (Exception e) {
                return "?";
            }
        }

        @JavascriptInterface
        public int versionCode() {
            try {
                return getPackageManager().getPackageInfo(getPackageName(), 0).versionCode;
            } catch (Exception e) {
                return 0;
            }
        }

        @JavascriptInterface
        public void openUrl(final String url) {
            if (url == null || !url.startsWith("https://")) return;
            runOnUiThread(new Runnable() {
                public void run() {
                    try {
                        startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                    } catch (Exception ignored) {
                    }
                }
            });
        }
    }

    // ------------------------------------------------------------------ certificates

    static String sha256(byte[] data) throws Exception {
        byte[] d = MessageDigest.getInstance("SHA-256").digest(data);
        StringBuilder sb = new StringBuilder();
        for (byte b : d) sb.append(String.format("%02x", b & 0xff));
        return sb.toString();
    }

    /** The certificate's SHA-256: getX509Certificate() on Android 10+, saveState() before that. */
    static String fingerprintOf(SslCertificate cert) {
        if (cert == null) return null;
        try {
            Object x = SslCertificate.class.getMethod("getX509Certificate").invoke(cert);
            if (x instanceof X509Certificate) return sha256(((X509Certificate) x).getEncoded());
        } catch (Exception ignored) {
            // older Android: fall through
        }
        try {
            Bundle b = SslCertificate.saveState(cert);
            byte[] der = b == null ? null : b.getByteArray("x509-certificate");
            return der == null ? null : sha256(der);
        } catch (Exception e) {
            return null;
        }
    }

    // ------------------------------------------------------------------ pairing

    /** POST /api/pair/claim on the computer; returns {token, fingerprint}. */
    static String[] claim(String server, String code) throws Exception {
        final String[] seen = new String[1];
        TrustManager capture = new X509TrustManager() {
            public void checkClientTrusted(X509Certificate[] chain, String auth) {
            }

            public void checkServerTrusted(X509Certificate[] chain, String auth) throws CertificateException {
                try {
                    seen[0] = sha256(chain[0].getEncoded());
                } catch (Exception e) {
                    throw new CertificateException(e);
                }
            }

            public X509Certificate[] getAcceptedIssuers() {
                return new X509Certificate[0];
            }
        };
        SSLContext ctx = SSLContext.getInstance("TLS");
        ctx.init(null, new TrustManager[]{capture}, null);
        HttpsURLConnection c = (HttpsURLConnection) new URL(server + "/api/pair/claim").openConnection();
        c.setSSLSocketFactory(ctx.getSocketFactory());
        c.setHostnameVerifier(new HostnameVerifier() {
            public boolean verify(String host, SSLSession session) {
                return true;  // checked below: the code's answer must name this exact certificate
            }
        });
        c.setConnectTimeout(6000);
        c.setReadTimeout(10000);
        c.setRequestMethod("POST");
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", "application/json");
        JSONObject body = new JSONObject();
        body.put("code", code);
        OutputStream out = c.getOutputStream();
        out.write(body.toString().getBytes("UTF-8"));
        out.close();
        int status = c.getResponseCode();
        InputStream in = status >= 400 ? c.getErrorStream() : c.getInputStream();
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        byte[] chunk = new byte[4096];
        int n;
        while (in != null && (n = in.read(chunk)) > 0) buf.write(chunk, 0, n);
        c.disconnect();
        JSONObject res = new JSONObject(buf.size() > 0 ? buf.toString("UTF-8") : "{}");
        if (status != HttpURLConnection.HTTP_OK) {
            throw new IllegalStateException(res.optString("error", "HTTP " + status));
        }
        String fp = res.getString("fingerprint");
        if (seen[0] == null || !fp.equalsIgnoreCase(seen[0])) {
            throw new IllegalStateException("certificate mismatch");
        }
        return new String[]{res.getString("token"), fp.toLowerCase()};
    }

    static String serverFrom(String address) {
        String a = address.trim().replaceFirst("^https?://", "").replaceAll("/.*$", "");
        if (a.isEmpty()) return null;
        if (!a.contains(":")) a = a + ":" + DEFAULT_PORT;
        return "https://" + a;
    }

    static String friendly(Exception e) {
        String m = String.valueOf(e.getMessage());
        if (e instanceof ConnectException || e instanceof SocketTimeoutException || e instanceof UnknownHostException
                || m.contains("ECONNREFUSED") || m.contains("EHOSTUNREACH") || m.contains("ENETUNREACH")) {
            return "Tietokoneeseen ei saada yhteyttä. Tarkista osoite, että puhelinyhteys on päällä "
                    + "tietokoneen Meetnotesissa ja että olet samassa Wi-Fi-verkossa.";
        }
        if (m.contains("attempts")) return "Liian monta väärää yritystä. Odota minuutti ja käytä uutta koodia.";
        if (m.contains("wrong or expired")) return "Väärä tai vanhentunut koodi. Katso uusi koodi tietokoneelta.";
        if (m.contains("forbidden host")) return "Kirjoita osoitteeksi tietokoneen IP-osoite, esim. 192.168.1.20.";
        if (m.contains("certificate mismatch")) return "Yhteys ei ole turvallinen: varmenne ei täsmää. Yritä uudelleen.";
        return "Yhdistäminen ei onnistunut: " + m;
    }

    // ------------------------------------------------------------------ native screens

    private int dp(int v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics());
    }

    private TextView text(String s, int sp, int color, boolean bold) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        t.setTextColor(color);
        t.setLineSpacing(0, 1.25f);
        if (bold) t.setTypeface(Typeface.DEFAULT_BOLD);
        return t;
    }

    private GradientDrawable box(int fill, int stroke, int radiusDp) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(fill);
        g.setCornerRadius(dp(radiusDp));
        if (stroke != 0) g.setStroke(dp(1), stroke);
        return g;
    }

    private EditText field(String hint, int inputType) {
        EditText e = new EditText(this);
        e.setHint(hint);
        e.setHintTextColor(MUTED);
        e.setTextColor(TEXT);
        e.setTextSize(TypedValue.COMPLEX_UNIT_SP, 20);
        e.setInputType(inputType);
        e.setSingleLine(true);
        e.setBackground(box(SURFACE, BORDER, 10));
        e.setPadding(dp(14), dp(12), dp(14), dp(12));
        return e;
    }

    private Button button(String label, boolean primary) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        b.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
        b.setTypeface(Typeface.DEFAULT_BOLD);
        b.setTextColor(primary ? ACCENT_INK : TEXT);
        b.setBackground(box(primary ? ACCENT : SURFACE, primary ? 0 : BORDER, 26));
        b.setMinHeight(dp(52));
        return b;
    }

    private LinearLayout page() {
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(dp(24), dp(40), dp(24), dp(32));
        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(BG);
        scroll.setFillViewport(true);
        scroll.addView(col);
        setContentView(scroll);
        return col;
    }

    private void gap(LinearLayout col, int h) {
        View v = new View(this);
        col.addView(v, new LinearLayout.LayoutParams(1, dp(h)));
    }

    private void showPairing(String message) {
        if (web != null) {
            web.destroy();
            web = null;
        }
        LinearLayout col = page();
        col.addView(text("Meetnotes", 30, TEXT, true));
        gap(col, 6);
        col.addView(text("Yhdistä tietokoneeseesi", 18, MUTED, false));
        gap(col, 24);
        col.addView(text("1. Avaa tietokoneella Meetnotes → Asetukset → Puhelin ja kytke puhelinyhteys päälle.\n"
                + "2. Valitse Android-sovellus. Näet osoitteen ja kuusinumeroisen koodin.\n"
                + "3. Puhelin ja tietokone pitää olla samassa Wi-Fi-verkossa.", 15, MUTED, false));
        gap(col, 24);

        col.addView(text("Osoite", 13, MUTED, true));
        gap(col, 6);
        final EditText address = field("192.168.1.20", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        String saved = getSharedPreferences(PREFS, MODE_PRIVATE).getString("last_address", "");
        address.setText(saved);
        col.addView(address);
        gap(col, 16);

        col.addView(text("Koodi", 13, MUTED, true));
        gap(col, 6);
        final EditText code = field("123 456", InputType.TYPE_CLASS_NUMBER);
        code.setImeOptions(EditorInfo.IME_ACTION_DONE);
        col.addView(code);
        gap(col, 24);

        final Button connect = button("Yhdistä", true);
        col.addView(connect, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dp(54)));
        gap(col, 16);

        final ProgressBar spinner = new ProgressBar(this);
        spinner.setVisibility(View.GONE);
        col.addView(spinner, new LinearLayout.LayoutParams(dp(32), dp(32)));
        final TextView status = text(message == null ? "" : message, 15, ERROR, false);
        col.addView(status);

        connect.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                final String server = serverFrom(address.getText().toString());
                final String c = code.getText().toString().replaceAll("\\D", "");
                if (server == null) {
                    status.setText("Kirjoita tietokoneen osoite.");
                    return;
                }
                if (c.length() != 6) {
                    status.setText("Koodissa on kuusi numeroa.");
                    return;
                }
                getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                        .putString("last_address", address.getText().toString().trim()).apply();
                connect.setEnabled(false);
                spinner.setVisibility(View.VISIBLE);
                status.setTextColor(MUTED);
                status.setText("Yhdistetään…");
                new Thread(new Runnable() {
                    public void run() {
                        try {
                            final String[] r = claim(server, c);
                            runOnUiThread(new Runnable() {
                                public void run() {
                                    savePairing(server, r[0], r[1]);
                                    openApp();
                                }
                            });
                        } catch (final Exception e) {
                            runOnUiThread(new Runnable() {
                                public void run() {
                                    connect.setEnabled(true);
                                    spinner.setVisibility(View.GONE);
                                    status.setTextColor(ERROR);
                                    status.setText(friendly(e));
                                }
                            });
                        }
                    }
                }).start();
            }
        });
    }

    private void showOffline(String message) {
        LinearLayout col = page();
        col.setGravity(Gravity.CENTER_VERTICAL);
        col.addView(text("Ei yhteyttä", 26, TEXT, true));
        gap(col, 12);
        col.addView(text(message, 16, MUTED, false));
        gap(col, 28);
        Button retry = button("Yritä uudelleen", true);
        col.addView(retry, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dp(54)));
        gap(col, 12);
        Button repair = button("Yhdistä uudelleen", false);
        col.addView(repair, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dp(54)));
        retry.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                openApp();
            }
        });
        repair.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                unpair();
            }
        });
    }
}

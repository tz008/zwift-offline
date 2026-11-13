package com.zoffline.solo;

import android.app.Activity;
import android.annotation.SuppressLint;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.view.ViewGroup;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.Toast;
import java.io.ByteArrayInputStream;

/** Local management page. No certificate-validation bypass or JavaScript bridge. */
public final class SettingsActivity extends Activity {
    private WebView web;
    @SuppressLint("SetJavaScriptEnabled") // Trusted local templates need JavaScript; no JS bridge or remote page navigation.
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                    insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            return insets;
        });
        Button back = new Button(this);
        back.setText(R.string.back_to_zoffline);
        back.setOnClickListener(view -> finish());
        content.addView(back);
        web = new WebView(this);
        web.getSettings().setJavaScriptEnabled(true);
        web.getSettings().setAllowFileAccess(false);
        web.getSettings().setAllowContentAccess(false);
        web.getSettings().setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        web.getSettings().setSupportMultipleWindows(false);
        web.setWebViewClient(new WebViewClient() {
            @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if ("http".equals(uri.getScheme()) && "127.0.0.1".equals(uri.getHost()) && uri.getPort() == 8081) return null;
                // The solo form has no external dependencies: do not wait for
                // the desktop template's CDN stylesheet while riding offline.
                return new WebResourceResponse("text/css", "UTF-8", new ByteArrayInputStream(new byte[0]));
            }
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if ("zwift".equals(uri.getHost())) {
                    Toast.makeText(SettingsActivity.this, "设置已保存，可返回打开 Zwift", Toast.LENGTH_LONG).show();
                    finish();
                    return true;
                }
                if ("http".equals(uri.getScheme()) && "127.0.0.1".equals(uri.getHost()) && uri.getPort() == 8081) {
                    return !("/user/zoffline/".equals(uri.getPath()) || "/ride".equals(uri.getPath())
                            || "/start-zwift".equals(uri.getPath()));
                }
                if ("https".equals(uri.getScheme())) {
                    try { startActivity(new Intent(Intent.ACTION_VIEW, uri)); } catch (RuntimeException ignored) {}
                }
                return true;
            }
            @Override public void onPageFinished(WebView view, String url) {
                // Bootstrap is optional; keep the local form usable with no internet.
                web.evaluateJavascript("(function(){if(document.getElementById('android-local-style'))return;"
                        + "var s=document.createElement('style');s.id='android-local-style';s.textContent="
                        + "'body{margin:18px;font:17px sans-serif;background:#f4f6f5!important;color:#19302c;overflow:auto!important}'"
                        + "+'input,select,button{font:inherit;max-width:100%;box-sizing:border-box;padding:10px;margin:5px 0}'"
                        + "+'select,.form-control{width:100%}a{display:none!important}.text-shadow{text-shadow:none!important}'"
                        + "+'form,h1{background:white!important}button,input[type=submit]{background:#00796b;color:white;border:0;border-radius:8px}'"
                        + "+'.row{display:flex;flex-wrap:wrap;gap:12px}.col-md-4,.col-md-6{flex:1;min-width:200px}'"
                        + ";document.head.appendChild(s);var m=document.createElement('meta');m.name='viewport';"
                        + "m.content='width=device-width,initial-scale=1';document.head.appendChild(m);"
                        + "var links=document.querySelectorAll('a');for(var i=0;i<links.length;i++)links[i].remove();"
                        + "var h=document.querySelector('h1');if(h)h.textContent='地图与幽灵骑手';"
                        + "var labels=document.querySelectorAll('label');var names={'Map':'地图','Climb':'爬坡路线','Enable ghosts':'启用幽灵骑手'};"
                        + "for(var j=0;j<labels.length;j++){var t=labels[j].textContent.trim();if(names[t])labels[j].textContent=names[t];}"
                        + "var b=document.querySelector('input[type=submit]');if(b)b.value='保存设置';})();", null);
            }
        });
        content.addView(web, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
        setContentView(content);
        web.loadUrl("http://127.0.0.1:8081/user/zoffline/");
    }
    @Override protected void onDestroy() {
        if (web != null) { web.stopLoading(); web.destroy(); }
        super.onDestroy();
    }
}

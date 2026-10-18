package com.example.hello;

import android.app.Activity;
import android.os.Bundle;
import android.os.Build;
import android.widget.TextView;
import android.util.TypedValue;

public class Hello extends Activity {
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        TextView tv = new TextView(this);
        tv.setText("Hello 零Gradle!\nAPI=" + Build.VERSION.SDK_INT
                + "  (" + Build.VERSION.RELEASE + ")\n"
                + Build.MANUFACTURER + " " + Build.MODEL);
        tv.setPadding(64, 160, 64, 64);
        tv.setTextSize(TypedValue.COMPLEX_UNIT_SP, 22);
        setContentView(tv);
    }
}

import java.io.*;
import java.util.zip.*;

// Append a classes.dex (STORED, so zipalign can 4-byte align it) into an existing APK,
// preserving all existing entries and their compression methods.
public class AddDex {
    public static void main(String[] a) throws Exception {
        if (a.length < 2) { System.err.println("usage: AddDex <apk> <dex>"); System.exit(2); }
        String apk = a[0], dex = a[1];
        File tmp = new File(apk + ".tmp");

        try (ZipInputStream zin = new ZipInputStream(new FileInputStream(apk))) {
            ZipOutputStream zout = new ZipOutputStream(new FileOutputStream(tmp));
            ZipEntry e;
            while ((e = zin.getNextEntry()) != null) {
                byte[] data;
                if (e.isDirectory()) {
                    zout.putNextEntry(new ZipEntry(e.getName()));
                    zout.closeEntry();
                    continue;
                }
                data = zin.readAllBytes();
                ZipEntry ne = new ZipEntry(e.getName());
                boolean stored = (e.getMethod() == ZipEntry.STORED);
                if (stored) {
                    long c = crc(data);
                    ne.setMethod(ZipEntry.STORED);
                    ne.setSize(data.length);
                    ne.setCompressedSize(data.length);
                    ne.setCrc(c);
                } else {
                    ne.setMethod(ZipEntry.DEFLATED);
                }
                zout.putNextEntry(ne);
                zout.write(data);
                zout.closeEntry();
            }
            // append dex as STORED
            byte[] dd = new FileInputStream(dex).readAllBytes();
            ZipEntry nd = new ZipEntry("classes.dex");
            nd.setMethod(ZipEntry.STORED);
            nd.setSize(dd.length);
            nd.setCompressedSize(dd.length);
            nd.setCrc(crc(dd));
            zout.putNextEntry(nd);
            zout.write(dd);
            zout.closeEntry();
            zout.close();
        }
        new File(apk).delete();
        if (!tmp.renameTo(new File(apk))) {
            System.err.println("rename failed");
            System.exit(3);
        }
        System.out.println("AddedDex OK");
    }

    static long crc(byte[] d) {
        CRC32 c = new CRC32();
        c.update(d);
        return c.getValue();
    }
}

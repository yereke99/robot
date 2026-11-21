/*
 * KalkanSigner — long-lived stdin/stdout signer that uses the same Kalkan
 * provider NCALayer ships, so our CMS PKCS#7 output is byte-compatible
 * with NURSign.
 *
 * Protocol (line-oriented JSON-ish, kept small to avoid JSON deps):
 *
 *   LOAD <p12_path> <base64_password>             -> OK | ERR <reason>
 *   SIGN_FILE <file_path>                         -> OK <base64_cms> | ERR
 *   SIGN_BYTES <base64_data>                      -> OK <base64_cms> | ERR
 *   SIGN_TEXT_TSA <base64_utf8>                   -> OK <base64_cms_with_tsa> | ERR
 *   SIGN_XML <base64_utf8_xml>                    -> OK <base64_utf8_signed_xml> | ERR
 *   PING                                          -> PONG
 *   QUIT                                          -> (exits)
 *
 * The SigningEntity is loaded ONCE per process. Subsequent SIGN_* calls
 * reuse it -- no popup, no password re-entry. This is the entire reason
 * for the helper to exist.
 */

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.security.Provider;
import java.security.Security;
import java.util.Base64;
import java.util.Enumeration;

import kz.gov.pki.kalkan.Storage;
import kz.gov.pki.kalkan.jce.provider.KalkanProvider;
import kz.gov.pki.kalkan.jce.provider.cms.CMSSignedData;
import kz.gov.pki.provider.utils.CMSUtil;
import kz.gov.pki.provider.utils.KeyStoreUtil;
import kz.gov.pki.provider.utils.XMLUtil;
import kz.gov.pki.provider.utils.model.SigningEntity;
import kz.gov.pki.provider.utils.model.TSAProfile;

public class KalkanSigner {
    private static SigningEntity entity;
    private static Provider provider;

    public static void main(String[] args) throws Exception {
        provider = new KalkanProvider();
        Security.addProvider(provider);
        // Initialize Apache xml-security with KZ GOST algorithm extensions.
        // KncaXS sets the pkigovkz config path, calls Init.init(), and
        // points JCEMapper at our KALKAN provider.
        // Reflective so we don't have to import it at compile time.
        try {
            Class.forName("kz.gov.pki.kalkan.xmldsig.KncaXS")
                 .getMethod("loadXMLSecurity")
                 .invoke(null);
        } catch (Throwable t) {
            System.err.println("WARN: KncaXS.loadXMLSecurity not available: " + t);
        }

        BufferedReader in = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
        PrintStream out = new PrintStream(System.out, true, "UTF-8");
        PrintStream err = new PrintStream(System.err, true, "UTF-8");

        err.println("KalkanSigner ready (provider=" + provider.getName()
                + " v" + provider.getVersion() + ")");

        String line;
        while ((line = in.readLine()) != null) {
            try {
                String[] parts = line.split(" ", 4);
                String cmd = parts[0];
                if ("PING".equals(cmd)) {
                    out.println("PONG");
                } else if ("LOAD".equals(cmd)) {
                    String p12Path = parts[1];
                    char[] password = new String(Base64.getDecoder().decode(parts[2]),
                                                 StandardCharsets.UTF_8).toCharArray();
                    loadEntity(p12Path, password);
                    out.println("OK " + describeEntity());
                } else if ("SIGN_FILE".equals(cmd)) {
                    String p = parts[1];
                    byte[] data = Files.readAllBytes(Paths.get(p));
                    String b64 = signBytes(data, false);
                    out.println("OK " + b64);
                } else if ("SIGN_BYTES".equals(cmd)) {
                    byte[] data = Base64.getDecoder().decode(parts[1]);
                    String b64 = signBytes(data, false);
                    out.println("OK " + b64);
                } else if ("SIGN_TEXT_TSA".equals(cmd)) {
                    byte[] data = Base64.getDecoder().decode(parts[1]);
                    String b64 = signBytes(data, true);
                    out.println("OK " + b64);
                } else if ("SIGN_XML".equals(cmd)) {
                    String xml = new String(Base64.getDecoder().decode(parts[1]),
                                            StandardCharsets.UTF_8);
                    String signedXml = signXml(xml);
                    String b64 = Base64.getEncoder()
                            .encodeToString(signedXml.getBytes(StandardCharsets.UTF_8));
                    out.println("OK " + b64);
                } else if ("QUIT".equals(cmd)) {
                    err.println("KalkanSigner exiting");
                    return;
                } else {
                    out.println("ERR unknown_command:" + cmd);
                }
            } catch (Throwable t) {
                String msg = t.getClass().getSimpleName() + ":" + t.getMessage();
                out.println("ERR " + msg.replace('\n', ' ').replace('\r', ' '));
                t.printStackTrace(err);
            }
        }
    }

    private static void loadEntity(String p12Path, char[] password) throws Exception {
        KeyStore ks = KeyStoreUtil.getKeyStore(Storage.PKCS12, p12Path, password, provider);
        // Find the first alias with a private key -- p12 from NCA usually has one.
        Enumeration<String> aliases = ks.aliases();
        String alias = null;
        while (aliases.hasMoreElements()) {
            String a = aliases.nextElement();
            if (ks.isKeyEntry(a)) { alias = a; break; }
        }
        if (alias == null) throw new RuntimeException("no_private_key_entry_in_p12");
        entity = KeyStoreUtil.getSigningEntity(ks, alias, password);
    }

    private static String describeEntity() {
        if (entity == null) return "no_entity";
        try {
            return "subject=" + entity.getCertificateChain().get(0)
                    .getSubjectX500Principal().getName();
        } catch (Exception e) {
            return "loaded";
        }
    }

    private static String signBytes(byte[] data, boolean withTsa) throws Exception {
        if (entity == null) throw new IllegalStateException("LOAD_required_first");
        CMSSignedData cms = CMSUtil.createCAdES(entity, data, false, false, provider);
        if (withTsa) {
            cms = CMSUtil.applyCAdEST(cms, entity, new TSAProfile(), provider);
        }
        return Base64.getEncoder().encodeToString(cms.getEncoded());
    }

    private static String signXml(String xml) throws Exception {
        if (entity == null) throw new IllegalStateException("LOAD_required_first");
        // XMLUtil.createXmlSignature is what NURSign uses for goszakup login.
        // Produces an XML-DSig signed envelope.
        return XMLUtil.createXmlSignature(entity, xml, provider);
    }
}

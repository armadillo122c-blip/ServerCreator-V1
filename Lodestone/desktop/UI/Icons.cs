using System.Globalization;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Documents;
using Avalonia.Controls.Shapes;
using Avalonia.Media;
using Path = Avalonia.Controls.Shapes.Path;

namespace Lodestone.Desktop.UI;

/// <summary>
/// Line icons on a 24-unit grid, drawn the way Modrinth's are (Lucide style:
/// 2-unit round strokes, no fill). Every path is written with spaced arc flags -
/// the compact "0 012-2" SVG form does not survive Avalonia's path parser.
/// </summary>
public static class Icons
{
    static string C(double cx, double cy, double r) =>
        string.Format(CultureInfo.InvariantCulture,
            "M{0} {1} a{2} {2} 0 1 0 {3} 0 a{2} {2} 0 1 0 {4} 0 ", cx - r, cy, r, 2 * r, -2 * r);

    static string R(double x, double y, double w, double h, double rx) =>
        string.Format(CultureInfo.InvariantCulture,
            "M{0} {1} h{2} a{3} {3} 0 0 1 {3} {3} v{4} a{3} {3} 0 0 1 {5} {3} h{6} a{3} {3} 0 0 1 {5} {5} v{7} a{3} {3} 0 0 1 {3} {5} z ",
            x + rx, y, w - 2 * rx, rx, h - 2 * rx, -rx, -(w - 2 * rx), -(h - 2 * rx));

    const string Folder = "M20 20 a2 2 0 0 0 2 -2 V8 a2 2 0 0 0 -2 -2 h-7.9 a2 2 0 0 1 -1.69 -0.9 L9.6 3.9 A2 2 0 0 0 7.93 3 H4 a2 2 0 0 0 -2 2 v13 a2 2 0 0 0 2 2 Z ";
    const string File = "M15 2 H6 a2 2 0 0 0 -2 2 v16 a2 2 0 0 0 2 2 h12 a2 2 0 0 0 2 -2 V7 Z M14 2 v4 a2 2 0 0 0 2 2 h4 ";
    const string Tray = "M21 15 v4 a2 2 0 0 1 -2 2 H5 a2 2 0 0 1 -2 -2 v-4 ";

    /// <summary>name -> (path data, filled)</summary>
    public static readonly Dictionary<string, (string d, bool fill)> All = new()
    {
        ["console"] = (R(3, 3, 18, 18, 3) + "M7 10 l3 2.5 l-3 2.5 M13 15 h4", false),
        ["folder"] = (Folder, false),
        ["folder-plus"] = (Folder + "M12 10 v6 M9 13 h6", false),
        ["file"] = (File, false),
        ["file-plus"] = (File + "M9 15 h6 M12 12 v6", false),
        ["file-text"] = (File + "M10 9 H8 M16 13 H8 M16 17 H8", false),
        ["sliders"] = ("M21 4 h-7 M10 4 H3 M21 12 h-9 M8 12 H3 M21 20 h-5 M12 20 H3 M14 2 v4 M8 10 v4 M16 18 v4", false),
        ["archive"] = (R(2, 3, 20, 5, 1) + "M4 8 v11 a2 2 0 0 0 2 2 h12 a2 2 0 0 0 2 -2 V8 M10 12 h4", false),
        ["wrench"] = ("M14.7 6.3 a1 1 0 0 0 0 1.4 l1.6 1.6 a1 1 0 0 0 1.4 0 l3.77 -3.77 a6 6 0 0 1 -7.94 7.94 l-6.91 6.91 a2.12 2.12 0 0 1 -3 -3 l6.91 -6.91 a6 6 0 0 1 7.94 -7.94 l-3.76 3.76 z", false),
        ["clock"] = (C(12, 12, 10) + "M12 6 v6 l4 2", false),
        ["sparkles"] = ("M9.94 15.5 A2 2 0 0 0 8.5 14.06 l-6.14 -1.58 a0.5 0.5 0 0 1 0 -0.96 L8.5 9.94 A2 2 0 0 0 9.94 8.5 l1.58 -6.14 a0.5 0.5 0 0 1 0.96 0 L14.06 8.5 A2 2 0 0 0 15.5 9.94 l6.14 1.58 a0.5 0.5 0 0 1 0 0.96 L15.5 14.06 a2 2 0 0 0 -1.44 1.44 l-1.58 6.14 a0.5 0.5 0 0 1 -0.96 0 z M20 3 v4 M22 5 h-4 M4 17 v2 M5 18 H3", false),
        ["discord"] = ("M19.3 5.4 A16.7 16.7 0 0 0 15.1 4 l-0.2 0.4 a12.6 12.6 0 0 1 3.7 1.9 a11.8 11.8 0 0 0 -4.3 -1.1 a12.4 12.4 0 0 0 -4.5 0 A11.8 11.8 0 0 0 5.4 6.3 A12.6 12.6 0 0 1 9.1 4.4 L8.9 4 a16.7 16.7 0 0 0 -4.2 1.4 C2.3 9 1.7 12.5 2 15.9 a16.8 16.8 0 0 0 5.1 2.6 l0.6 -1 a11 11 0 0 1 -1.7 -0.8 l0.4 -0.3 a11.9 11.9 0 0 0 11.2 0 l0.4 0.3 a11 11 0 0 1 -1.7 0.8 l0.6 1 a16.8 16.8 0 0 0 5.1 -2.6 c0.4 -4 -0.6 -7.4 -2.7 -10.5 z M9 14 c-0.8 0 -1.5 -0.8 -1.5 -1.7 S8.2 10.6 9 10.6 s1.5 0.8 1.5 1.7 S9.8 14 9 14 z M15 14 c-0.8 0 -1.5 -0.8 -1.5 -1.7 s0.7 -1.7 1.5 -1.7 s1.5 0.8 1.5 1.7 S15.8 14 15 14 z", true),
        ["home"] = ("M15 21 v-8 a1 1 0 0 0 -1 -1 h-4 a1 1 0 0 0 -1 1 v8 M3 10 a2 2 0 0 1 0.71 -1.53 l7 -6 a2 2 0 0 1 2.58 0 l7 6 A2 2 0 0 1 21 10 v9 a2 2 0 0 1 -2 2 H5 a2 2 0 0 1 -2 -2 Z", false),
        ["grid"] = (R(3, 3, 7, 7, 1) + R(14, 3, 7, 7, 1) + R(14, 14, 7, 7, 1) + R(3, 14, 7, 7, 1), false),
        ["server"] = (R(2, 2, 20, 8, 2) + R(2, 14, 20, 8, 2) + "M6 6 h0.01 M6 18 h0.01", false),
        ["globe"] = (C(12, 12, 10) + "M12 2 a14.5 14.5 0 0 0 0 20 a14.5 14.5 0 0 0 0 -20 M2 12 h20", false),
        ["code"] = ("M16 18 l6 -6 l-6 -6 M8 6 l-6 6 l6 6", false),
        ["settings"] = ("M12.22 2 h-0.44 a2 2 0 0 0 -2 2 v0.18 a2 2 0 0 1 -1 1.73 l-0.43 0.25 a2 2 0 0 1 -2 0 l-0.15 -0.08 a2 2 0 0 0 -2.73 0.73 l-0.22 0.38 a2 2 0 0 0 0.73 2.73 l0.15 0.1 a2 2 0 0 1 1 1.72 v0.51 a2 2 0 0 1 -1 1.74 l-0.15 0.09 a2 2 0 0 0 -0.73 2.73 l0.22 0.38 a2 2 0 0 0 2.73 0.73 l0.15 -0.08 a2 2 0 0 1 2 0 l0.43 0.25 a2 2 0 0 1 1 1.73 V20 a2 2 0 0 0 2 2 h0.44 a2 2 0 0 0 2 -2 v-0.18 a2 2 0 0 1 1 -1.73 l0.43 -0.25 a2 2 0 0 1 2 0 l0.15 0.08 a2 2 0 0 0 2.73 -0.73 l0.22 -0.39 a2 2 0 0 0 -0.73 -2.73 l-0.15 -0.08 a2 2 0 0 1 -1 -1.74 v-0.5 a2 2 0 0 1 1 -1.74 l0.15 -0.09 a2 2 0 0 0 0.73 -2.73 l-0.22 -0.38 a2 2 0 0 0 -2.73 -0.73 l-0.15 0.08 a2 2 0 0 1 -2 0 l-0.43 -0.25 a2 2 0 0 1 -1 -1.73 V4 a2 2 0 0 0 -2 -2 z " + C(12, 12, 3), false),
        ["play"] = ("M6 3.5 L19.5 12 L6 20.5 Z", true),
        ["stop"] = (R(5, 5, 14, 14, 2.5), true),
        ["restart"] = ("M21 12 a9 9 0 1 1 -9 -9 c2.52 0 4.93 1 6.74 2.74 L21 8 M21 3 v5 h-5", false),
        ["zap"] = ("M4 14 a1 1 0 0 1 -0.78 -1.63 l9.9 -10.2 a0.5 0.5 0 0 1 0.86 0.46 l-1.92 6.02 A1 1 0 0 0 13 10 h7 a1 1 0 0 1 0.78 1.63 l-9.9 10.2 a0.5 0.5 0 0 1 -0.86 -0.46 l1.92 -6.02 A1 1 0 0 0 11 14 z", false),
        ["plus"] = ("M5 12 h14 M12 5 v14", false),
        ["upload"] = (Tray + "M17 8 l-5 -5 l-5 5 M12 3 v12", false),
        ["download"] = (Tray + "M7 10 l5 5 l5 -5 M12 15 V3", false),
        ["refresh"] = ("M3 12 a9 9 0 0 1 9 -9 a9.75 9.75 0 0 1 6.74 2.74 L21 8 M21 3 v5 h-5 M21 12 a9 9 0 0 1 -9 9 a9.75 9.75 0 0 1 -6.74 -2.74 L3 16 M8 16 H3 v5", false),
        ["trash"] = ("M3 6 h18 M19 6 v14 c0 1 -1 2 -2 2 H7 c-1 0 -2 -1 -2 -2 V6 M8 6 V4 c0 -1 1 -2 2 -2 h4 c1 0 2 1 2 2 v2", false),
        ["edit"] = ("M21.17 6.81 a1 1 0 0 0 -3.99 -3.99 L3.84 16.17 a2 2 0 0 0 -0.5 0.83 l-1.32 4.35 a0.5 0.5 0 0 0 0.62 0.62 l4.35 -1.32 a2 2 0 0 0 0.83 -0.5 z", false),
        ["copy"] = (R(8, 8, 14, 14, 2) + "M4 16 c-1.1 0 -2 -0.9 -2 -2 V4 c0 -1.1 0.9 -2 2 -2 h10 c1.1 0 2 0.9 2 2", false),
        ["x"] = ("M18 6 L6 18 M6 6 l12 12", false),
        ["check"] = ("M20 6 L9 17 l-5 -5", false),
        ["chevron-down"] = ("M6 9 l6 6 l6 -6", false),
        ["chevron-right"] = ("M9 18 l6 -6 l-6 -6", false),
        ["arrow-left"] = ("M12 19 l-7 -7 l7 -7 M19 12 H5", false),
        ["arrow-up"] = ("M5 12 l7 -7 l7 7 M12 19 V5", false),
        ["send"] = ("M14.54 21.69 a0.5 0.5 0 0 0 0.94 -0.02 l6.5 -19 a0.5 0.5 0 0 0 -0.64 -0.64 l-19 6.5 a0.5 0.5 0 0 0 -0.02 0.94 l7.93 3.18 a2 2 0 0 1 1.11 1.11 z M21.85 2.15 l-10.94 10.94", false),
        ["paperclip"] = ("M21.44 11.05 l-9.19 9.19 a6 6 0 0 1 -8.49 -8.49 l8.57 -8.57 A4 4 0 1 1 18 8.84 l-8.59 8.57 a2 2 0 0 1 -2.83 -2.83 l8.49 -8.48", false),
        ["save"] = ("M15.2 3 a2 2 0 0 1 1.4 0.6 l3.8 3.8 a2 2 0 0 1 0.6 1.4 V19 a2 2 0 0 1 -2 2 H5 a2 2 0 0 1 -2 -2 V5 a2 2 0 0 1 2 -2 z M17 21 v-7 a1 1 0 0 0 -1 -1 H8 a1 1 0 0 0 -1 1 v7 M7 3 v4 a1 1 0 0 0 1 1 h7", false),
        ["maximize"] = ("M15 3 h6 v6 M9 21 H3 v-6 M21 3 l-7 7 M3 21 l7 -7", false),
        ["minimize"] = ("M4 14 h6 v6 M20 10 h-6 V4 M14 10 l7 -7 M3 21 l7 -7", false),
        ["wrap"] = ("M3 6 h18 M3 12 h15 a3 3 0 1 1 0 6 h-4 M16 16 l-2 2 l2 2 M3 18 h7", false),
        ["users"] = ("M16 21 v-2 a4 4 0 0 0 -4 -4 H6 a4 4 0 0 0 -4 4 v2 " + C(9, 7, 4) + "M22 21 v-2 a4 4 0 0 0 -3 -3.87 M16 3.13 a4 4 0 0 1 0 7.75", false),
        ["user-plus"] = ("M16 21 v-2 a4 4 0 0 0 -4 -4 H6 a4 4 0 0 0 -4 4 v2 " + C(9, 7, 4) + "M19 8 v6 M22 11 h-6", false),
        ["logout"] = ("M9 21 H5 a2 2 0 0 1 -2 -2 V5 a2 2 0 0 1 2 -2 h4 M16 17 l5 -5 l-5 -5 M21 12 H9", false),
        ["ban"] = (C(12, 12, 10) + "M4.9 4.9 l14.2 14.2", false),
        ["activity"] = ("M22 12 h-4 l-3 9 L9 3 l-3 9 H2", false),
        ["timer"] = ("M10 2 h4 M12 14 l3 -3 " + C(12, 14, 8), false),
        ["message"] = ("M21 15 a2 2 0 0 1 -2 2 H7 l-4 4 V5 a2 2 0 0 1 2 -2 h14 a2 2 0 0 1 2 2 z", false),
        ["alert"] = ("M21.73 18 l-8 -14 a2 2 0 0 0 -3.48 0 l-8 14 A2 2 0 0 0 4 21 h16 a2 2 0 0 0 1.73 -3 M12 9 v4 M12 17 h0.01", false),
        ["info"] = (C(12, 12, 10) + "M12 16 v-4 M12 8 h0.01", false),
        ["circle-check"] = (C(12, 12, 10) + "M9 12 l2 2 l4 -4", false),
        ["circle-x"] = (C(12, 12, 10) + "M15 9 l-6 6 M9 9 l6 6", false),
        ["external"] = ("M15 3 h6 v6 M10 14 L21 3 M18 13 v6 a2 2 0 0 1 -2 2 H5 a2 2 0 0 1 -2 -2 V8 a2 2 0 0 1 2 -2 h6", false),
        ["power"] = ("M12 2 v10 M18.4 6.6 a9 9 0 1 1 -12.77 0.04", false),
        ["key"] = (C(7.5, 15.5, 5.5) + "M21 2 l-9.6 9.6 M15.5 7.5 l3 3 L22 7 l-3 -3", false),
        ["shield"] = ("M20 13 c0 5 -3.5 7.5 -7.66 8.95 a1 1 0 0 1 -0.67 -0.01 C7.5 20.5 4 18 4 13 V6 a1 1 0 0 1 1 -1 c2 0 4.5 -1.2 6.24 -2.72 a1.17 1.17 0 0 1 1.52 0 C14.51 3.81 17 5 19 5 a1 1 0 0 1 1 1 z", false),
        ["search"] = (C(11, 11, 8) + "M21 21 l-4.3 -4.3", false),
        ["filter"] = ("M22 3 H2 l8 9.46 V19 l4 2 v-8.54 L22 3 z", false),
        ["box"] = ("M21 8 a2 2 0 0 0 -1 -1.73 l-7 -4 a2 2 0 0 0 -2 0 l-7 4 A2 2 0 0 0 3 8 v8 a2 2 0 0 0 1 1.73 l7 4 a2 2 0 0 0 2 0 l7 -4 A2 2 0 0 0 21 16 Z M3.3 7 l8.7 5 l8.7 -5 M12 22 V12", false),
        ["moon"] = ("M12 3 a6 6 0 0 0 9 9 a9 9 0 1 1 -9 -9 Z", false),
        ["plug"] = ("M12 22 v-5 M9 8 V2 M15 8 V2 M18 8 v5 a4 4 0 0 1 -4 4 h-4 a4 4 0 0 1 -4 -4 V8 Z", false),
        ["unplug"] = ("M19 5 l3 -3 M2 22 l3 -3 M6.3 20.3 a2.4 2.4 0 0 0 3.4 0 L12 18 l-6 -6 l-2.3 2.3 a2.4 2.4 0 0 0 0 3.4 Z M7.5 13.5 L10 11 M10.5 16.5 L13 14 M12 6 l6 6 l2.3 -2.3 a2.4 2.4 0 0 0 0 -3.4 l-2.6 -2.6 a2.4 2.4 0 0 0 -3.4 0 Z", false),
        ["drive"] = ("M22 12 H2 M5.45 5.11 L2 12 v6 a2 2 0 0 0 2 2 h16 a2 2 0 0 0 2 -2 v-6 l-3.45 -6.89 A2 2 0 0 0 16.76 4 H7.24 a2 2 0 0 0 -1.79 1.11 z M6 16 h0.01 M10 16 h0.01", false),
        ["import"] = ("M12 17 V3 M6 11 l6 6 l6 -6 M19 21 H5", false),
        ["more"] = (C(12, 12, 1) + C(19, 12, 1) + C(5, 12, 1), false),
        ["cpu"] = (R(4, 4, 16, 16, 2) + R(9, 9, 6, 6, 1) + "M15 2 v2 M15 20 v2 M2 15 h2 M2 9 h2 M20 15 h2 M20 9 h2 M9 2 v2 M9 20 v2", false),
        ["mountain"] = ("M8 3 l4 8 l5 -5 l5 15 H2 L8 3 z", false),
        ["puzzle"] = ("M15.39 4.39 a1 1 0 0 0 1.68 -0.47 a2.5 2.5 0 1 1 3.01 3.01 a1 1 0 0 0 -0.47 1.68 l1.68 1.68 a2.41 2.41 0 0 1 0 3.41 l-1.68 1.69 a1 1 0 0 1 -1.68 -0.47 a2.5 2.5 0 1 0 -3.01 3.01 a1 1 0 0 1 0.47 1.68 l-1.68 1.68 a2.41 2.41 0 0 1 -3.41 0 l-1.69 -1.68 a1 1 0 0 0 -1.68 0.47 a2.5 2.5 0 1 1 -3.01 -3.01 a1 1 0 0 0 0.47 -1.68 l-1.68 -1.68 a2.41 2.41 0 0 1 0 -3.41 l1.68 -1.69 a1 1 0 0 1 1.68 0.47 a2.5 2.5 0 1 0 3.01 -3.01 a1 1 0 0 1 -0.47 -1.68 l1.68 -1.68 a2.41 2.41 0 0 1 3.41 0 z", false),
        ["list"] = ("M3 12 h0.01 M3 18 h0.01 M3 6 h0.01 M8 12 h13 M8 18 h13 M8 6 h13", false),
        ["hash"] = ("M4 9 h16 M4 15 h16 M10 3 L8 21 M16 3 l-2 18", false),
        ["webhook"] = ("M18 16.98 h-5.99 c-1.1 0 -1.95 0.94 -2.48 1.9 A4 4 0 0 1 2 17 c0.01 -0.7 0.2 -1.4 0.57 -2 M6 17 l3.13 -5.78 c0.53 -0.97 0.1 -2.18 -0.5 -3.1 a4 4 0 1 1 6.89 -4.06 M12 6 l3.13 5.73 C15.66 12.7 16.9 13 18 13 a4 4 0 0 1 0 8", false),
        ["layers"] = ("M12.83 2.18 a2 2 0 0 0 -1.66 0 L2.6 6.08 a1 1 0 0 0 0 1.83 l8.58 3.91 a2 2 0 0 0 1.66 0 l8.58 -3.9 a1 1 0 0 0 0 -1.83 Z M2 12 a1 1 0 0 0 0.58 0.91 l8.6 3.91 a2 2 0 0 0 1.65 0 l8.58 -3.9 A1 1 0 0 0 22 12 M2 17 a1 1 0 0 0 0.58 0.91 l8.6 3.91 a2 2 0 0 0 1.65 0 l8.58 -3.9 A1 1 0 0 0 22 17", false),
        ["gauge"] = ("M12 14 l4 -4 M3.34 19 a10 10 0 1 1 17.32 0", false),
        ["eye"] = ("M2.06 12.35 a1 1 0 0 1 0 -0.7 a10.75 10.75 0 0 1 19.88 0 a1 1 0 0 1 0 0.7 a10.75 10.75 0 0 1 -19.88 0 " + C(12, 12, 3), false),
        ["type"] = ("M4 7 V4 h16 v3 M9 20 h6 M12 4 v16", false),
        ["wifi"] = ("M12 20 h0.01 M2 8.82 a15 15 0 0 1 20 0 M5 12.86 a10 10 0 0 1 14 0 M8.5 16.43 a5 5 0 0 1 7 0", false),
        ["package"] = ("M11 21.73 a2 2 0 0 0 2 0 l7 -4 A2 2 0 0 0 21 16 V8 a2 2 0 0 0 -1 -1.73 l-7 -4 a2 2 0 0 0 -2 0 l-7 4 A2 2 0 0 0 3 8 v8 a2 2 0 0 0 1 1.73 z M12 22 V12 M3.29 7 L12 12 l8.71 -5 M7.5 4.27 l9 5.15", false),
        ["image"] = (R(3, 3, 18, 18, 2) + C(9, 9, 2) + "M21 15 l-3.09 -3.09 a2 2 0 0 0 -2.82 0 L6 21", false),
        ["bot"] = ("M12 8 V4 H8 " + R(4, 8, 16, 12, 2) + "M2 14 h2 M20 14 h2 M15 13 v2 M9 13 v2", false),
        ["user"] = ("M19 21 v-2 a4 4 0 0 0 -4 -4 H9 a4 4 0 0 0 -4 4 v2 " + C(12, 7, 4), false),
    };

    public static Control Make(string name, double size = 18, IBrush? brush = null, double stroke = 2)
    {
        if (!All.TryGetValue(name, out var def))
            def = All["box"];
        var path = new Path
        {
            Data = Geometry.Parse(def.d),
            StrokeThickness = stroke,
            StrokeLineCap = PenLineCap.Round,
            StrokeJoin = PenLineJoin.Round,
            Stretch = Stretch.None,
        };
        if (brush != null)
        {
            if (def.fill) path.Fill = brush;
            else path.Stroke = brush;
        }
        else
        {
            // Follow the text colour of whatever holds the icon, so an icon in a
            // brand button turns black with its label and back again on hover.
            path.Bind(def.fill ? Shape.FillProperty : Shape.StrokeProperty,
                path.GetObservable(TextElement.ForegroundProperty));
        }
        var canvas = new Canvas { Width = 24, Height = 24, Children = { path } };
        return new Viewbox
        {
            Width = size,
            Height = size,
            Stretch = Stretch.Uniform,
            Child = canvas,
            VerticalAlignment = Avalonia.Layout.VerticalAlignment.Center,
        };
    }

    /// <summary>Every icon must parse; checked once by --shots.</summary>
    public static List<string> Broken()
    {
        var bad = new List<string>();
        foreach (var (k, v) in All)
        {
            try { Geometry.Parse(v.d); }
            catch (Exception) { bad.Add(k); }
        }
        return bad;
    }
}

using System;

namespace AotForms
{
    internal static class Offsets
    {
        // ================= STATIC / BASE =================

        internal static uint InitBase           = 0xA342EFC;
        internal static uint GameFacade_Script  = 0xA5BC344;
        internal static uint Access_StaticClass  = 0x5C;

        // ================= MATCH / GAME STATE =================

        internal static uint m_Match            = 0x50;
        internal static uint CurrentMatchGame   = 0x4;
        internal static uint MatchState         = 0x8C;
        internal static uint GameTimer          = 0x10;
        internal static uint FixedDeltaTime     = 0x24;
        internal static uint Level              = 0x14;

        // ================= PLAYER =================

        internal static uint LocalPlayer         = 0x94;
        internal static uint LocalPlayerAttrib   = 0x500;
        internal static uint DictionaryEntities  = 0x68;
        internal static uint Player_Name          = 0x31C;
        internal static uint Player_IsDead        = 0x50;
        internal static uint Player_Data_Pool     = 0x48;
        internal static uint Player_ShadowBase    = 0x1A60;
        internal static uint IsClientBot          = 0x324;

        // ================= AVATAR =================

        internal static uint Avatar               = 0xA8;
        internal static uint AvatarManager        = 0x504;
        internal static uint Avatar_Data           = 0x14;
        internal static uint Avatar_Data_IsTeam    = 0x59;
        internal static uint Avatar_IsVisible      = 0x95;

        // ================= CAMERA =================

        internal static uint Camera_Intptr        = 0x8;
        internal static uint MainCameraTransform  = 0x28C;
        internal static uint Camera               = 0x18;
        internal static uint FollowCamera         = 0x494;

        // ================= OBSERVER =================

        internal static uint ObserverPlayer       = 0x28;
        internal static uint CurrentObserver      = 0xB4;

        // ================= WEAPON =================

        internal static uint Weapon                = 0x434;
        internal static uint WeaponData            = 0x58;
        internal static uint WeaponInfo_Type       = 0x64;
        internal static uint WeaponRecoil_Scatter  = 0xC;
        internal static uint RapidfirePtr          = 0x1C8;
        internal static uint NoReloadPtr           = 0xC1;

        // ================= AIM =================

        internal static uint AimbotVisible        = 0x4E8;
        internal static uint AimRotation          = 0x440;
        internal static uint saim1                = 0x58C;
        internal static uint saim2                = 0x9D0;
        internal static uint sAim3                = 0x38;
        internal static uint sAim4                = 0x2C;
    }
}

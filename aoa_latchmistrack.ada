procedure Set_Latch_Mistrack is


   MAX_AOA_MISTRACK_PERS_CNT : constant BASE_DATA_TYPE.SHORT_INTEGER := 100;
begin
    if AOA_MISTRACK_UNLTCH1_LIST(L1) then
        if(AOA_MISTRACK_PERS_CNT(L1) >= MAX_AOA_MISTRACK_PERS_CNT) then
           aoa_mistrack_ltch_list(L1) := TRUE;
        else
           AOA_MISTRACK_PERS_CNT(L1) := AOA_MISTRACK_PERS_CNT(L1) + 1;
        end if;
    else 
          AOA_MISTRACK_PERS_CNT(L1) := 0;
    end if;

    if AOA_MISTRACK_UNLTCH1_LIST(R1) then
        if(AOA_MISTRACK_PERS_CNT(R1) >= MAX_AOA_MISTRACK_PERS_CNT) then
           aoa_mistrack_ltch_list(R1) := TRUE;
        else
           AOA_MISTRACK_PERS_CNT(R1) := AOA_MISTRACK_PERS_CNT(R1) + 1;
        end if;
    else 
          AOA_MISTRACK_PERS_CNT(R1) := 0;
    end if;
end Set_Latch_Mistrack;